"""Strict, bounded readers for two native flattened pin-net export formats.

These exporters share Siemens' database/flattening engine. Agreement is a format
cross-check, not an independent electrical solver or a proof of full ERC.
"""

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .diagnostics import fingerprint
from .geometry import canonical
from .sessions import PadsError

REF = r"[A-Za-z][A-Za-z0-9_]{0,63}"
NUMBER = r"[A-Za-z0-9_+\-]{1,64}"
ATOM = r"[^\s,:;()'\"]{1,256}"
SECTIONS = ("COMPPROPS", "COMPPINS", "E-NETS", "BUSES", "NETS", "GROUND", "POWER")
FORMAT = "pads_sct_allegro_pin_membership_v1"


def require(condition, message):
    if not condition:
        raise PadsError("invalid_logical_export", message)


def validate_pins(pins):
    if (
        not isinstance(pins, list)
        or not 1 <= len(pins) <= 16
        or any(
            not isinstance(p, dict)
            or set(p) != {"refdes", "pin_number"}
            or not isinstance(p["refdes"], str)
            or not re.fullmatch(REF, p["refdes"])
            or not isinstance(p["pin_number"], str)
            or not re.fullmatch(NUMBER, p["pin_number"])
            for p in pins
        )
    ):
        raise PadsError(
            "invalid_logical_pins",
            "Require 1..16 exact reference designator / pin-number pairs",
        )
    if len({(p["refdes"], p["pin_number"]) for p in pins}) != len(pins):
        raise PadsError("invalid_logical_pins", "Duplicate requested pin")


def sections(text):
    require(
        "\x00" not in text and len(text) <= 16 * 1024 * 1024,
        "Invalid export text size/encoding",
    )
    result, active, expected = {}, None, 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("###### "):
            continue
        if line.startswith("BEGIN_"):
            name = line[6:]
            require(
                active is None
                and expected < len(SECTIONS)
                and name == SECTIONS[expected],
                "Unexpected/duplicate SCT section",
            )
            active = name
            result[name] = []
        elif line.startswith("END_"):
            require(
                active is not None and line == "END_" + active,
                "Mismatched SCT end marker",
            )
            active = None
            expected += 1
        else:
            require(active is not None, "Content outside SCT sections")
            result[active].append(line)
    require(active is None and expected == len(SECTIONS), "Incomplete SCT export")
    return result


def parse_sct(text):
    data = sections(text)
    declared = set()
    for line in data["COMPPROPS"]:
        ref, sep, values = line.partition(":")
        require(
            sep and re.fullmatch(REF, ref) and ref not in declared,
            "Invalid/duplicate component declaration",
        )
        try:
            fields = next(
                csv.reader([values], delimiter=",", quotechar="'", strict=True)
            )
        except csv.Error as exc:
            raise PadsError(
                "invalid_logical_export", "Malformed SCT component properties"
            ) from exc
        require(len(fields) == 4, "Unexpected SCT property layout")
        declared.add(ref)
    require(0 < len(declared) <= 5000, "Component count outside supported range")
    components, all_pins, repeated, pin_labels = {}, set(), {}, {}
    for line in data["COMPPINS"]:
        m = re.fullmatch(r"(" + REF + r")\((.*)\)", line)
        require(m is not None, "Malformed SCT component pins")
        ref, body = m.groups()
        require(
            ref in declared and ref not in components,
            "Unknown/duplicate component pin declaration",
        )
        matches = list(re.finditer(r"\((" + NUMBER + r"):([^():,;]*)\)", body))
        require(
            matches and ",".join(m.group(0) for m in matches) == body,
            "Unsupported or incomplete pin syntax",
        )
        numbers = [m.group(1) for m in matches]
        labels = {}
        for pin in matches:
            number, label = pin.groups()
            require(
                number not in labels or labels[number] == label,
                "Conflicting repeated pin declaration",
            )
            labels[number] = label
        repeated[ref] = sorted({n for n in numbers if numbers.count(n) > 1})
        pin_labels[ref] = labels
        components[ref] = sorted(set(numbers))
        all_pins.update((ref, n) for n in numbers)
    require(
        set(components) == declared and len(all_pins) <= 100000,
        "Incomplete component pin coverage",
    )
    # E-NETS can describe a broader electrical grouping (e.g. through passives).
    # Validate its syntax, but never use it to merge the physical NETS membership.
    aliases = set()
    for line in data["E-NETS"]:
        name, sep, members = line.partition(":")
        require(
            sep
            and re.fullmatch(ATOM, name)
            and all(re.fullmatch(ATOM, v) for v in members.split(",")),
            "Unsupported E-NETS syntax",
        )
        aliases.update(members.split(","))
    for line in data["BUSES"]:
        require(
            all(re.fullmatch(ATOM, v) for v in line.split(",")),
            "Unsupported bus-name syntax",
        )
    nets, occupied = {}, set()
    for section in ("NETS", "GROUND", "POWER"):
        for line in data[section]:
            name, sep, tail = line.partition(":")
            require(
                sep
                and re.fullmatch(ATOM, name)
                and name not in nets
                and tail.endswith(";"),
                "Malformed/duplicate SCT net",
            )
            body = tail[:-1]
            matches = list(
                re.finditer(r"(" + REF + r")\((" + NUMBER + r"):([^():,;]*)\)", body)
            )
            require(
                matches and ",".join(m.group(0) for m in matches) == body,
                "Incomplete/unsupported SCT net members",
            )
            pins = [(m.group(1), m.group(2)) for m in matches]
            require(
                all(p in all_pins for p in pins)
                and len(set(pins)) == len(pins)
                and not occupied.intersection(pins),
                "Unknown, duplicate, or multiply assigned pin",
            )
            occupied.update(pins)
            nets[name] = sorted(pins)
    require(
        0 < len(nets) <= 10000 and set(nets).issubset(aliases),
        "Missing SCT nets or electrical declarations",
    )
    return {
        "components": components,
        "nets": nets,
        "repeated": repeated,
        "pin_labels": pin_labels,
    }


def parse_allegro(text):
    require(
        "\x00" not in text and len(text) <= 16 * 1024 * 1024,
        "Invalid Allegro export size/encoding",
    )
    headers = re.findall(r"^\$[^\r\n]+", text, re.M)
    require(
        headers
        == ["$PACKAGES", "$A_PROPERTIES", "$NETS", "$PINS", "$A_PROPERTIES", "$END"],
        "Unsupported/incomplete Allegro sections",
    )
    require(not text.split("$END", 1)[1].strip(), "Trailing Allegro data")
    body = text.split("$NETS", 1)[1].split("$PINS", 1)[0]
    nets, occupied = {}, set()
    current = None
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^'(" + ATOM + r")'\s*;\s*(.*)$", line)
        if m:
            current, line = m.groups()
            require(current not in nets, "Duplicate Allegro net")
            nets[current] = []
        require(current is not None, "Allegro member before net declaration")
        for token in line.replace(",", " ").split():
            m = re.fullmatch(r"(" + REF + r")\.(" + NUMBER + r")", token)
            require(m is not None, "Unsupported Allegro member")
            pin = m.groups()
            require(pin not in occupied, "Duplicate/multiply assigned Allegro pin")
            occupied.add(pin)
            nets[current].append(pin)
    require(
        nets and len(nets) <= 10000 and all(nets.values()), "Empty Allegro connectivity"
    )
    return {k: sorted(v) for k, v in nets.items()}


def graph(sct, allegro):
    parsed = parse_sct(sct)
    require(
        parsed["nets"] == parse_allegro(allegro),
        "Native export formats disagree about pin membership",
    )
    return normalized_sct(parsed)


def normalized_sct(parsed):
    return {
        "components": [
            {
                "refdes": r,
                "pin_numbers": p,
                "repeated_pin_declarations": parsed["repeated"][r],
            }
            for r, p in sorted(parsed["components"].items())
        ],
        "nets": [
            {"name": n, "pins": [{"refdes": r, "pin_number": p} for r, p in pins]}
            for n, pins in sorted(parsed["nets"].items())
        ],
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def export_round(database, design, output_dir, *, _candidate_only=False):
    """Fixed installed read exporters, fresh outputs, no public executable/options."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    sdd = Path(os.environ["SDD_HOME"]).resolve(strict=True)
    texts, evidence = {}, {}
    formats = [("SctNetlist", "sct.txt")]
    if not _candidate_only:
        formats.append(("AllegroNetlist", "allegro.txt"))
    for name, filename in formats:
        executable = sdd / "wv/win32/bin" / (name + ".exe")
        source_sha = fingerprint(executable)
        path = output_dir / filename
        command = [
            str(executable),
            "-block=" + design,
            "-icdbdir=" + str(database),
            "-icdbsnapshot=DxD",
            "-flowtype=DX",
            "-out=" + str(path),
        ]
        try:
            r = subprocess.run(
                command,
                cwd=output_dir,
                env=dict(os.environ),
                capture_output=True,
                timeout=25,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            (output_dir / (name + "-timeout.json")).write_text(
                json.dumps({"timeout_seconds": 25})
            )
            raise PadsError(
                "logical_export_timeout",
                "Native export timed out; no verified report recorded",
            ) from exc
        (output_dir / (name + "-stdout.txt")).write_bytes(r.stdout)
        (output_dir / (name + "-stderr.txt")).write_bytes(r.stderr)
        unpackaged = b"Design is not packaged!" in r.stdout + r.stderr
        if unpackaged and not _candidate_only:
            raise PadsError(
                "logical_design_unpacked",
                "Native logical export requires packaged components. This read tool does not package or assign pins; recover the pending component changes or explicitly prepare packaging before inspecting again.",
            )
        if (
            r.returncode != 0
            or any(
                stream.strip()
                and not (
                    _candidate_only
                    and stream.strip() == b"Error: Design is not packaged!"
                )
                for stream in (r.stdout, r.stderr)
            )
            or not path.is_file()
            or not 0 < path.stat().st_size <= 16 * 1024 * 1024
        ):
            raise PadsError(
                "logical_export_incomplete",
                "Native export was missing, oversized or reported diagnostics; exit zero alone is insufficient",
            )
        require(
            fingerprint(executable) == source_sha,
            "Native exporter changed during reading",
        )
        texts[name] = path.read_bytes().decode("mbcs", errors="strict")
        evidence[name] = {
            "unpackaged_candidate": unpackaged,
            "executable": str(executable),
            "executable_sha256": source_sha,
            "output": str(path),
            "output_sha256": fingerprint(path),
        }
    value = (
        normalized_sct(parse_sct(texts["SctNetlist"]))
        if _candidate_only
        else graph(texts["SctNetlist"], texts["AllegroNetlist"])
    )
    return value, evidence


def compare_reports(before, after):
    if before["provenance"] != after["provenance"]:
        raise PadsError(
            "logical_baseline_changed",
            "Native exporters or parser contract differ from baseline",
        )
    a, b = before["netlist"], after["netlist"]
    return {
        "netlist_equal": a == b,
        "selected_pins_equal": before["resolved_pins"] == after["resolved_pins"],
        "nets_before_only": [n for n in a["nets"] if n not in b["nets"]],
        "nets_after_only": [n for n in b["nets"] if n not in a["nets"]],
        "components_equal": a["components"] == b["components"],
    }
