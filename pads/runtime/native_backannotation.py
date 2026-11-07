"""Fixed BAM-only native proposals and live preimage checks, never BAM application."""

import os
from pathlib import Path
import re
import shutil
import subprocess

from .backannotation import ATTRIBUTES, parse_bam, impact
from .diagnostics import fingerprint
from .geometry import canonical
from .logical import export_round, parse_sct
from .native_designer import call
from .native_packaging import protected_files
from .packaging import validate_output
from .sessions import PadsError


def read_state(app, design, records=()):
    selected = {(r["scope"], r["uid"]): {p["name"] for p in r["pins"]} for r in records}
    rows, pins = [], []
    for i, component in enumerate(
        call(app, "DesignComponents", "", design, "-1", "", True)
    ):
        if i >= 1000:
            raise PadsError(
                "backannotation_limit", "At most 1000 scanned component objects"
            )
        ref = str(component.Refdes)
        if not ref:
            continue
        parent = str(call(component.Parent, "GetName", 0))
        sheet = str(component.Parent.SheetNum)
        scope = (
            design
            + ("!" + parent.replace("\\", "!") if parent != design else "")
            + "("
            + sheet
            + ")"
        )
        uid = str(component.UID)
        attrs = {}
        for name in ATTRIBUTES:
            a = call(component, "FindAttribute", name)
            attrs[name] = (
                None
                if a is None
                else {
                    "base": str(a.Value),
                    "instance": str(a.InstanceValue),
                    "effective": str(a.EitherValue),
                }
            )
        rows.append(
            {
                "scope": scope,
                "uid": uid,
                "path": str(call(component, "GetName", 0)),
                "refdes": ref,
                "attributes": attrs,
            }
        )
        wanted = selected.get((scope, uid), set())
        if wanted:
            for connection in call(component, "GetConnections"):
                cp = connection.CompPin
                name = str(call(cp.Pin, "GetName", 1))
                if name in wanted:
                    net = connection.Net
                    if net and call(connection.Segment, "IsBus"):
                        raise PadsError(
                            "backannotation_unsupported",
                            "Direct bus-segment pin preimages are unsupported",
                        )
                    pins.append(
                        {
                            "scope": scope,
                            "uid": uid,
                            "name": name,
                            "pin_uid": str(cp.Pin.UID),
                            "number": str(cp.Number),
                            "net_uid": str(net.UID) if net else None,
                        }
                    )
    return canonical(rows), canonical(pins)


def run_proposal(project, design, output, mode):
    if mode not in ("add", "repackage_unfixed"):
        raise PadsError("invalid_backannotation_mode", "Select a fixed preview mode")
    output.mkdir(parents=True, exist_ok=False)
    source = project.parent / "Integration/LocalPartsDB.pdb"
    pdb = output / "PartsDB.pdb"
    digest = fingerprint(source)
    shutil.copy2(source, pdb)
    if fingerprint(pdb) != digest:
        raise PadsError("backannotation_source_changed", "PDB changed during copying")
    executable = Path(os.environ["SDD_HOME"]) / "wg/win32/bin/package.exe"
    tool_hash = fingerprint(executable)
    bam, log = output / "changes.bam", output / "package.log"
    command = [
        str(executable),
        "-j" + str(project),
        "-n" + design,
        "-Add",
        "-NoFill",
        "-p" + str(pdb),
        "-b" + str(bam),
        "-l" + str(log),
        "-CW",
        "-y",
    ]
    if mode == "repackage_unfixed":
        command.append("-r")
    try:
        run = subprocess.run(
            command,
            cwd=project.parent,
            env=dict(os.environ),
            capture_output=True,
            timeout=40,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise PadsError(
            "backannotation_outcome_unknown",
            "BAM-only process timed out; inspect the retained copy",
        ) from exc
    (output / "stdout.txt").write_bytes(run.stdout)
    (output / "stderr.txt").write_bytes(run.stderr)
    if not all(p.is_file() and p.stat().st_size <= 1024 * 1024 for p in (bam, log)):
        raise PadsError(
            "backannotation_incomplete", "Missing or oversized BAM/log output"
        )
    # Reuse only the diagnostics gate. The original executable packaging route
    # still passes the actual BAM to its empty-BAM-only validator.
    validate_output(
        run.returncode,
        run.stdout.decode("mbcs"),
        run.stderr.decode("mbcs"),
        log.read_bytes().decode("mbcs"),
        b"",
        True,
    )
    if fingerprint(source) != digest or fingerprint(executable) != tool_hash:
        raise PadsError("backannotation_source_changed", "PDB or packager changed")
    records = parse_bam(bam.read_bytes().decode("mbcs", errors="strict"))
    if records:
        counts = set(
            re.findall(
                r"There have been (\d+) reference designator changes and (\d+) pin\s+number changes",
                log.read_bytes().decode("mbcs"),
            )
        )
        expected = (
            str(sum(r["refdes"] is not None for r in records)),
            str(sum(len(r["pins"]) for r in records)),
        )
        if counts != {expected}:
            raise PadsError(
                "backannotation_incomplete",
                "BAM records disagree with native change counts",
            )
    return records, {
        "bam": str(bam),
        "bam_sha256": fingerprint(bam),
        "bam_bytes": bam.stat().st_size,
        "log": str(log),
        "log_sha256": fingerprint(log),
        "working_pdb": str(pdb),
        "working_pdb_sha256": fingerprint(pdb),
        "source_pdb_sha256": digest,
        "executable_sha256": tool_hash,
        "mode": mode,
        "bam_only": True,
    }


def preview(app, request, identity, initial):
    auxiliary = False
    try:
        root = Path(request["policy"]["fixture"]).resolve(strict=True)
        project = Path(request["expected_path"]).resolve(strict=True)
        lists = re.findall(
            r"(?ms)^LIST PDBs\s*\r?\n(.*?)^ENDLIST\s*$",
            project.read_bytes().decode("mbcs"),
        )
        if not lists or any(s.strip() for s in lists):
            raise PadsError(
                "backannotation_source_unsupported",
                "Additional project PDB paths are unsupported",
            )
        data = call(app, "GetProjectData")
        designs = call(data, "GetiCDBDesigns")
        design = request["design"]
        if design not in [
            str(call(designs, "GetItem", i))
            for i in range(1, call(designs, "GetCount") + 1)
        ]:
            raise PadsError("design_not_found", "An existing design is required")
        database, library = Path(data.iCDBDir).resolve(strict=True), Path(
            data.CentralLibraryPath
        ).resolve(strict=True)
        pdb = (project.parent / "Integration/LocalPartsDB.pdb").resolve(strict=True)
        if not all(p.is_relative_to(root) for p in (database, library, pdb)):
            raise PadsError(
                "backannotation_source_unsupported",
                "Native sources must remain in the test copy",
            )
        protected = protected_files(root)
        before, _ = read_state(app, design)
        output = Path(request["output_dir"])
        graph, graph_evidence = export_round(database, design, output / "before")
        labels = parse_sct(
            Path(graph_evidence["SctNetlist"]["output"]).read_bytes().decode("mbcs")
        )["pin_labels"]
        if identity() != initial:
            raise PadsError("target_changed", "Application changed before preflight")
        auxiliary = True
        records, first = run_proposal(
            project, design, output / "first", request["mode"]
        )
        observed, pins = read_state(app, design, records)
        if observed != before:
            raise PadsError(
                "backannotation_snapshot_changed",
                "Live identities or observed properties changed",
            )
        report = impact(records, before, pins, graph, labels)
        repeated, second = run_proposal(
            project, design, output / "second", request["mode"]
        )
        after, after_pins = read_state(app, design, records)
        after_graph, after_evidence = export_round(database, design, output / "after")
        after_labels = parse_sct(
            Path(after_evidence["SctNetlist"]["output"]).read_bytes().decode("mbcs")
        )["pin_labels"]
        tools = {k: v["executable_sha256"] for k, v in graph_evidence.items()}
        if (
            before != after
            or pins != after_pins
            or graph != after_graph
            or labels != after_labels
            or protected_files(root) != protected
            or identity() != initial
            or Path(call(app, "GetProjectData").iCDBDir).resolve() != database
            or Path(call(app, "GetProjectData").CentralLibraryPath).resolve() != library
            or tools != {k: v["executable_sha256"] for k, v in after_evidence.items()}
            or first["source_pdb_sha256"] != second["source_pdb_sha256"]
            or first["executable_sha256"] != second["executable_sha256"]
            or fingerprint(Path(os.environ["SDD_HOME"]) / "wg/win32/bin/package.exe")
            != first["executable_sha256"]
        ):
            raise PadsError(
                "backannotation_snapshot_changed",
                "Proposal, live preimages, graph or sources changed; no valid impact baseline",
            )
        variants = []
        first_by_key = {(r["scope"], r["uid"]): r for r in records}
        second_by_key = {(r["scope"], r["uid"]): r for r in repeated}
        for key in sorted(first_by_key.keys() | second_by_key.keys()):
            a, b = first_by_key.get(key), second_by_key.get(key)
            if a != b:
                variants.append(
                    {"scope": key[0], "uid": key[1], "first": a, "second": b}
                )
        if variants:
            report["recovery"]["blockers"].append("native_proposal_not_repeatable")
        return {
            "ok": True,
            "identity": initial,
            "impact": report,
            "proposal_records": records,
            "repeatability": {
                "identical": not variants,
                "changed_instances": variants,
                "note": "First proposal is frozen evidence, not a promise that a later packager run will reproduce it.",
            },
            "live_baseline": before,
            "pin_preimages": pins,
            "netlist_baseline": graph,
            "provenance": {
                "format": report["format"],
                "pdb_sha256": first["source_pdb_sha256"],
                "packager_sha256": first["executable_sha256"],
                "exporters": tools,
            },
            "evidence": {
                "first": first,
                "second": second,
                "before": graph_evidence,
                "after": after_evidence,
            },
            "protected_files_unchanged": len(protected),
            "observed_state_unchanged": True,
            "schematic_update_attempted": False,
            "project_write_attempted": True,
            "auxiliary_files_written": True,
            "write_authorized": False,
            "executable_preview": False,
            "recovery_verified": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "kind": (
                    exc.kind
                    if isinstance(exc, PadsError)
                    else "native_backannotation_failed"
                ),
                "message": (
                    str(exc)
                    if isinstance(exc, PadsError)
                    else "BAM-only preflight failed; retain the copy for inspection"
                ),
            },
            "project_write_attempted": auxiliary,
            "auxiliary_files_written": auxiliary,
            "schematic_update_attempted": False,
            "schematic_update_outcome_unknown": auxiliary,
        }
