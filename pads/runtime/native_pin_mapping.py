"""Corroborate selected live instances with native project PDB and netlist exports."""

import os
from pathlib import Path
import re
import shutil
import subprocess

from .diagnostics import fingerprint
from .geometry import canonical
from .logical import export_round, parse_sct
from .native_designer import call
from .native_packaging import protected_files
from .pin_mapping import FORMAT, validate_refs, parse_part, resolve_reference
from .sessions import PadsError


def read_instances(app, design, refdes):
    validate_refs(refdes)
    result, count, pins_seen = [], 0, 0
    for component in call(app, "DesignComponents", "", design, "-1", "", True):
        count += 1
        if count > 10000:
            raise PadsError(
                "design_too_large", "Mapping limit: 10000 scanned component objects"
            )
        ref = str(component.Refdes)
        if ref not in refdes:
            continue
        attribute = call(component, "FindAttribute", "Part Number")
        number = str(attribute.EitherValue) if attribute else ""
        if not number or len(number) > 128 or any(ord(c) < 32 for c in number):
            raise PadsError(
                "mapping_part_unavailable",
                "An explicit effective Part Number is required",
            )
        pins = []
        for connection in call(component, "GetConnections"):
            pins_seen += 1
            if pins_seen > 128:
                raise PadsError(
                    "mapping_too_large",
                    "Select at most 128 live pins across queried instances",
                )
            cp, net = connection.CompPin, connection.Net
            if net and call(connection.Segment, "IsBus"):
                raise PadsError(
                    "mapping_unsupported", "Direct bus-segment mapping is not supported"
                )
            pins.append(
                {
                    "uid": str(cp.Pin.UID),
                    "name": str(call(cp.Pin, "GetName", 1)),
                    "number": str(cp.Number),
                    "net_uid": str(net.UID) if net else None,
                    "net_name": str(net.LogicalNetName) if net else None,
                }
            )
        row = {
            "refdes": ref,
            "uid": str(component.UID),
            "path": str(call(component, "GetName", 0)),
            "block_path": str(call(component.Parent, "GetName", 0)),
            "sheet": str(component.Parent.SheetNum),
            "symbol": str(call(component.SymbolBlock, "GetName", 0)),
            "partition": str(component.SymbolBlock.LibraryName),
            "part_number": number,
            "pins": canonical(pins),
        }
        if (
            any(
                not row[k]
                for k in ("uid", "path", "block_path", "sheet", "symbol", "partition")
            )
            or not pins
            or any(not p["uid"] or not p["name"] for p in pins)
            or len({p["uid"] for p in pins}) != len(pins)
        ):
            raise PadsError(
                "mapping_identity_incomplete",
                "Live instance or pin identity is incomplete",
            )
        result.append(row)
        if len(result) > 16:
            raise PadsError(
                "mapping_too_large", "Mapping limit: 16 selected symbol instances"
            )
    if set(refdes) != {c["refdes"] for c in result} or len(
        {(c["path"], c["sheet"]) for c in result}
    ) != len(result):
        raise PadsError(
            "mapping_reference_not_found",
            "Requested references are missing or instance paths are ambiguous",
        )
    return canonical(result)


def export_part(pdb, number, output):
    output.mkdir(parents=True, exist_ok=False)
    base = Path(os.environ["SDD_HOME"]) / "common/win32/_bin"
    native, plain = output / "part.hkp", output / "part.plain.hkp"
    sources = {}
    for tool, args, codes in (
        (
            "PartsDB2HKP",
            ["-i", str(pdb), "-o", str(native), "-n", number, "-u", "mm"],
            {0},
        ),
        ("DataConvert", ["-i", str(native), "-o", str(plain)], {0, 1}),
    ):
        exe = base / (tool + ".exe")
        digest = fingerprint(exe)
        try:
            run = subprocess.run(
                [str(exe), *args],
                cwd=output,
                env=dict(os.environ),
                capture_output=True,
                timeout=25,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise PadsError(
                "mapping_export_timeout",
                "Read-only part export timed out; no mapping baseline recorded",
            ) from exc
        (output / (tool + "-stdout.txt")).write_bytes(run.stdout)
        (output / (tool + "-stderr.txt")).write_bytes(run.stderr)
        target = native if tool == "PartsDB2HKP" else plain
        if (
            run.returncode not in codes
            or run.stdout.strip()
            or run.stderr.strip()
            or not target.is_file()
            or not 0 < target.stat().st_size <= 4 * 1024 * 1024
            or fingerprint(exe) != digest
        ):
            raise PadsError(
                "mapping_export_incomplete",
                "Native part export failed, changed, or produced diagnostics/invalid output",
            )
        sources[tool] = {"sha256": digest, "exit_code": run.returncode}
    # DataConvert's exit code 1 alone is never proof of success.
    part = parse_part(plain.read_bytes().decode("mbcs", errors="strict"), number)
    return part, {
        "tools": sources,
        "native_output": str(native),
        "native_sha256": fingerprint(native),
        "plain_output": str(plain),
        "plain_sha256": fingerprint(plain),
    }


def inspect(app, request, identity, initial):
    refs, design = request["refdes"], request["design"]
    validate_refs(refs)
    root = Path(request["policy"]["fixture"]).resolve(strict=True)
    project = Path(request["expected_path"]).resolve(strict=True)
    lists = re.findall(
        r"(?ms)^LIST PDBs\s*\r?\n(.*?)^ENDLIST\s*$", project.read_bytes().decode("mbcs")
    )
    if not lists or any(s.strip() for s in lists):
        raise PadsError(
            "mapping_source_unsupported",
            "Additional project PDB search paths are not supported",
        )
    data = call(app, "GetProjectData")
    designs = call(data, "GetiCDBDesigns")
    if design not in [
        str(call(designs, "GetItem", n))
        for n in range(1, call(designs, "GetCount") + 1)
    ]:
        raise PadsError("design_not_found", "Select an existing design")
    database = Path(data.iCDBDir).resolve(strict=True)
    library = Path(data.CentralLibraryPath).resolve(strict=True)
    pdb = (project.parent / "Integration/LocalPartsDB.pdb").resolve(strict=True)
    if (
        not all(p.is_relative_to(root) for p in (database, library, pdb))
        or pdb.stat().st_size > 64 * 1024 * 1024
    ):
        raise PadsError(
            "mapping_source_unsupported",
            "Mapping sources must stay inside the supported test copy",
        )
    protected = protected_files(root)
    source_sha = fingerprint(pdb)
    before = read_instances(app, design, refs)
    part_numbers = {c["part_number"] for c in before}
    if len(part_numbers) > 4:
        raise PadsError(
            "mapping_too_large", "At most four effective parts may be exported"
        )
    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=False)
    copied = output / "project-parts.pdb"
    shutil.copy2(pdb, copied)
    if fingerprint(copied) != source_sha:
        raise PadsError("mapping_source_changed", "Project PDB changed during copying")
    parts, part_evidence = {}, {}
    for i, number in enumerate(sorted(part_numbers)):
        parts[number], part_evidence[number] = export_part(
            copied, number, output / ("part-" + str(i))
        )
    first, first_evidence = export_round(database, design, output / "first")
    labels = parse_sct(
        Path(first_evidence["SctNetlist"]["output"]).read_bytes().decode("mbcs")
    )["pin_labels"]
    results = []
    for ref in sorted(refs):
        selected = [c for c in before if c["refdes"] == ref]
        numbers = {c["part_number"] for c in selected}
        if len(numbers) != 1:
            results.append(
                {
                    "refdes": ref,
                    "status": "needs_review",
                    "issues": ["conflicting_effective_part_numbers"],
                    "all_selected_live_instances_corroborated": False,
                    "write_authorized": False,
                }
            )
        else:
            results.append(
                resolve_reference(
                    ref, selected, parts[next(iter(numbers))], first, labels
                )
            )
    after = read_instances(app, design, refs)
    second, second_evidence = export_round(database, design, output / "second")
    after_labels = parse_sct(
        Path(second_evidence["SctNetlist"]["output"]).read_bytes().decode("mbcs")
    )["pin_labels"]
    netlist_tools = {k: v["executable_sha256"] for k, v in first_evidence.items()}
    if (
        before != after
        or first != second
        or labels != after_labels
        or protected_files(root) != protected
        or fingerprint(pdb) != source_sha
        or fingerprint(copied) != source_sha
        or identity() != initial
        or Path(call(app, "GetProjectData").iCDBDir).resolve() != database
        or Path(call(app, "GetProjectData").CentralLibraryPath).resolve() != library
        or netlist_tools
        != {k: v["executable_sha256"] for k, v in second_evidence.items()}
    ):
        raise PadsError(
            "mapping_snapshot_changed",
            "Live instances, graph or protected sources changed while reading",
        )
    tool_hashes = {}
    for evidence in part_evidence.values():
        for name, info in evidence["tools"].items():
            if (
                fingerprint(
                    Path(os.environ["SDD_HOME"]) / "common/win32/_bin" / (name + ".exe")
                )
                != info["sha256"]
                or name in tool_hashes
                and tool_hashes[name] != info["sha256"]
            ):
                raise PadsError(
                    "mapping_source_changed", "Part exporter changed during inspection"
                )
            tool_hashes[name] = info["sha256"]
    return {
        "ok": True,
        "identity": initial,
        "mapping": results,
        "all_selected_live_instances_corroborated": all(
            r["all_selected_live_instances_corroborated"] for r in results
        ),
        "provenance": {
            "format": FORMAT,
            "project_pdb_sha256": source_sha,
            "part_exporters": tool_hashes,
            "netlist_exporters": netlist_tools,
        },
        "source": {
            "kind": "project_integration_local_partsdb",
            "path": str(pdb),
            "central_library_equivalence_verified": False,
        },
        "parts": parts,
        "evidence": {
            "parts": part_evidence,
            "first": first_evidence,
            "second": second_evidence,
        },
        "protected_files_unchanged": len(protected),
        "project_write_attempted": False,
        "write_authorized": False,
        "full_design_pin_mapping_verified": False,
        "full_erc_verified": False,
        "note": "Library-slot evidence for selected references only. Shared physical pins and library supplies are not unique live write endpoints. No pin assignment, packaging, source-library update or manufacturer pinout approval.",
    }
