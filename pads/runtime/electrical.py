"""Reproducible local connectivity reports and a bounded native Verify profile."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from .diagnostics import fingerprint
from .geometry import canonical
from .routing import netlist
from .sessions import PadsError

CHECKS = ("UnconnectedPins", "ZeroPin", "NetOverlap")


def verify_profile(definitions):
    # Preserve the installed definition file's byte syntax. The old native
    # reader distinguishes definition files (-def) from user settings (-set).
    data = re.sub(
        rb'(<Check\b[^>]*\bState=")Enabled("[^>]*>)', rb"\1Disabled\2", definitions
    )
    for name in CHECKS:
        data, count = re.subn(
            rb'(<Check Name="' + name.encode() + rb'"[^>]*State=")Disabled',
            rb"\1Enabled",
            data,
        )
        if count != 1:
            raise PadsError(
                "native_check_profile_changed",
                "Installed Verify definitions differ from the supported profile",
            )
    root = ET.fromstring(data)
    if {
        c.get("Name") for c in root.iter("Check") if c.get("State") == "Enabled"
    } != set(CHECKS):
        raise PadsError(
            "native_check_profile_changed", "Unexpected enabled native checks"
        )
    return data


def native_connectivity(app, sheet):
    from .native_designer import call

    result = netlist(sheet)
    actual = []
    for net in call(app.ActiveView, "Query", 32, 0):
        pins = []
        for conn in call(net, "Connections", ""):
            cp = conn.CompPin
            c = cp.Component
            pins.append(
                {
                    "target": {
                        "design": sheet["design"],
                        "sheet": str(c.Parent.SheetNum),
                        "uid": str(c.UID),
                        "path": str(call(c, "GetName", 3)),
                        "refdes": str(c.Refdes),
                    },
                    "pin_uid": str(cp.Pin.UID),
                    "number": str(cp.Number),
                }
            )
        actual.append(
            {
                "uid": str(net.UID),
                "name": str(net.LogicalNetName),
                "pins": canonical(pins),
            }
        )
    if canonical(actual) != result["nets"]:
        raise PadsError(
            "connectivity_mismatch",
            "Native net-to-pin and component-to-net queries disagree",
        )
    result["bidirectional_native_read_verified"] = True
    result["sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True).encode()
    ).hexdigest()
    return result


def parse_verify_output(stdout, stderr, code):
    text = stdout.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(
        re.finditer(r"(\d+) Error\(s\), (\d+) Warning\(s\), (\d+) Note\(s\),?", text)
    )
    if (
        code != 0
        or stderr.strip()
        or len(matches) != 1
        or text[matches[0].end() :].strip()
    ):
        raise PadsError(
            "native_check_incomplete",
            "Verify did not produce one recognized complete summary; inspect evidence",
        )
    prefix = text[: matches[0].start()].strip()
    if re.search(r"(?im)^\s*(?:Error\s*:|Usage:|Warning:)", prefix):
        raise PadsError(
            "native_check_incomplete", "Verify reported an invocation/database error"
        )
    counts = dict(zip(("errors", "warnings", "notes"), map(int, matches[0].groups())))
    if (any(counts.values()) and not prefix) or (not any(counts.values()) and prefix):
        raise PadsError(
            "native_check_incomplete", "Verify summary and findings are inconsistent"
        )
    return {
        "status": "completed",
        "counts": counts,
        "findings_text": prefix,
        "profile_clean": not any(counts.values()),
        "full_erc_verified": False,
    }


def run_verify(project, design, sheet, output_dir):
    sdd = Path(os.environ["SDD_HOME"]).resolve(strict=True)
    executable = sdd / "wv/win32/bin/vdrc.exe"
    defaults = sdd / "standard/VerifyDefaults.ini"
    inputs = {str(p): fingerprint(p) for p in (executable, defaults)}
    profile = verify_profile(defaults.read_bytes())
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    config = output_dir / "connectivity.ini"
    config.write_bytes(profile)
    settings = output_dir / "settings.ini"
    settings.write_bytes(
        b'<?xml version="1.0"?>\n<VDRC><FlowType Name="Expedition"/><GUI><Settings/><Rules/></GUI></VDRC>'
    )
    profile_sha, settings_sha = fingerprint(config), fingerprint(settings)
    command = [
        str(executable),
        "-proj",
        str(project),
        "-block",
        design,
        "-page",
        sheet,
        "-set",
        str(settings),
        "-def",
        str(config),
    ]
    try:
        r = subprocess.run(
            command,
            cwd=output_dir,
            env=dict(os.environ),
            capture_output=True,
            timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        (output_dir / "timeout.json").write_text(json.dumps({"timeout_seconds": 90}))
        raise PadsError(
            "native_check_timeout",
            "Native Verify exceeded 90 seconds; no passing result recorded",
        ) from exc
    (output_dir / "stdout.txt").write_bytes(r.stdout)
    (output_dir / "stderr.txt").write_bytes(r.stderr)
    if (
        any(fingerprint(Path(p)) != sha for p, sha in inputs.items())
        or fingerprint(config) != profile_sha
        or fingerprint(settings) != settings_sha
    ):
        raise PadsError(
            "native_check_inputs_changed",
            "Verify executable or source settings changed during check",
        )
    result = parse_verify_output(
        r.stdout.decode("mbcs", errors="replace"),
        r.stderr.decode("mbcs", errors="replace"),
        r.returncode,
    )
    result.update(
        profile="pads_connectivity_v1",
        enabled_checks=list(CHECKS),
        input_sha256=inputs,
        profile_sha256=profile_sha,
        settings_sha256=settings_sha,
        evidence=str(output_dir),
        note="Three explicit native checks only. Project Verify.ini is not modified or used as a claim of full ERC.",
    )
    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


def compare_checks(before, after):
    # Compare only matching provenance and scope. Different rules are not a valid baseline.
    if (
        before["native_verify"]["profile_sha256"]
        != after["native_verify"]["profile_sha256"]
        or before["native_verify"].get("settings_sha256")
        != after["native_verify"].get("settings_sha256")
        or before["native_verify"]["input_sha256"]
        != after["native_verify"]["input_sha256"]
    ):
        raise PadsError(
            "check_baseline_changed",
            "Checker or rule settings differ from the baseline",
        )
    a, b = before["connectivity"], after["connectivity"]
    return {
        "netlist_equal": a == b,
        "nets_before_only": [n for n in a["nets"] if n not in b["nets"]],
        "nets_after_only": [n for n in b["nets"] if n not in a["nets"]],
        "unconnected_before_only": [
            p for p in a["unconnected_pins"] if p not in b["unconnected_pins"]
        ],
        "unconnected_after_only": [
            p for p in b["unconnected_pins"] if p not in a["unconnected_pins"]
        ],
        "native_counts_delta": {
            k: after["native_verify"]["counts"][k]
            - before["native_verify"]["counts"][k]
            for k in ("errors", "warnings", "notes")
        },
        "native_findings_equal": before["native_verify"]["findings_text"]
        == after["native_verify"]["findings_text"],
    }
