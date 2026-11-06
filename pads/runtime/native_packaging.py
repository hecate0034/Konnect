"""Fixed native Add/NoFill packaging, with an empty-BAM-only preflight."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .diagnostics import fingerprint
from .geometry import canonical
from .logical import FORMAT, export_round
from .native_designer import call
from .native_logical import read_selected, resolve_membership
from .packaging import validate_output
from .sessions import PadsError


def protected_files(root):
    result = {}
    for p in root.rglob("*"):
        if p.is_file() and (
            p.relative_to(root).parts[0] == "Library"
            or "PCB" in p.relative_to(root).parts
            or p.suffix.lower() in (".prj", ".ini", ".dbc", ".ipl", ".pdb", ".lmc")
            or p.name == "source-manifest.json"
        ):
            result[str(p.relative_to(root))] = fingerprint(p)
    return result


def assignments(app, design):
    # Pin membership is checked in the native exported graph. Enumerating every
    # live CompPin here is too slow over COM; only the new pins are corroborated.
    rows = []
    count = 0
    for component in call(app, "DesignComponents", "", design, "-1", "", True):
        count += 1
        if count > 1000:
            raise PadsError(
                "design_too_large", "Packaging limit: 1000 live component objects"
            )
        refdes = str(component.Refdes)
        if refdes:
            rows.append(
                {
                    "uid": str(component.UID),
                    "path": str(call(component, "GetName", 0)),
                    "refdes": refdes,
                    "sheet": str(component.Parent.SheetNum),
                }
            )
    return canonical(rows)


def run_package(project, design, output, preview):
    output.mkdir(parents=True, exist_ok=False)
    source = project.parent / "Integration/LocalPartsDB.pdb"
    target = output / "PartsDB.pdb"
    shutil.copy2(source, target)
    executable = Path(os.environ["SDD_HOME"]) / "wg/win32/bin/package.exe"
    digest = fingerprint(executable)
    bam, log = output / "changes.bam", output / "package.log"
    command = [
        str(executable),
        "-j" + str(project),
        "-n" + design,
        "-Add",
        "-NoFill",
        "-p" + str(target),
        "-b" + str(bam),
        "-l" + str(log),
        "-CW",
    ]
    if preview:
        command.append("-y")
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
            "packaging_outcome_unknown",
            "Native packager timed out; inspect the test copy and do not retry",
        ) from exc
    (output / "stdout.txt").write_bytes(run.stdout)
    (output / "stderr.txt").write_bytes(run.stderr)
    if (
        not log.is_file()
        or not bam.is_file()
        or log.stat().st_size > 4 * 1024 * 1024
        or bam.stat().st_size > 4 * 1024 * 1024
    ):
        raise PadsError(
            "packaging_incomplete", "Missing or oversized native packaging outputs"
        )
    validate_output(
        run.returncode,
        run.stdout.decode("mbcs"),
        run.stderr.decode("mbcs"),
        log.read_bytes().decode("mbcs"),
        bam.read_bytes(),
        preview,
    )
    if fingerprint(executable) != digest:
        raise PadsError("packaging_source_changed", "Packager changed during execution")
    return {
        "executable_sha256": digest,
        "log": str(log),
        "log_sha256": fingerprint(log),
        "bam": str(bam),
        "bam_sha256": fingerprint(bam),
        "bam_bytes": bam.stat().st_size,
        "working_pdb": str(target),
        "working_pdb_sha256": fingerprint(target),
        "mode": "Add/NoFill/BAM-only" if preview else "Add/NoFill/update-iCDB",
    }


def operate(app, request, identity, initial):
    # Keep failure outcomes separate from the surrounding sheet driver's write flag.
    from .native_schematic import read_sheet

    update = auxiliary = False
    try:
        root = Path(request["policy"]["fixture"]).resolve(strict=True)
        project = Path(request["expected_path"]).resolve(strict=True)
        text = project.read_bytes().decode("mbcs")
        pdb_lists = re.findall(r"(?ms)^LIST PDBs\s*\r?\n(.*?)^ENDLIST\s*$", text)
        if not pdb_lists or any(v.strip() for v in pdb_lists):
            raise PadsError(
                "packaging_scope_denied",
                "Additional project PDB search paths are not supported",
            )
        data = call(app, "GetProjectData")
        database = Path(data.iCDBDir).resolve(strict=True)
        library = Path(data.CentralLibraryPath).resolve(strict=True)
        if not database.is_relative_to(root) or not library.is_relative_to(root):
            raise PadsError(
                "write_scope_denied", "Native packaging sources left the fixture"
            )
        protected = protected_files(root)
        design, sheet = request["design"], request["sheet"]
        current = read_sheet(app, design, sheet)
        if current != request["expected_sheet"]:
            raise PadsError("stale_preview", "Sheet differs from recorded placement")
        if call(app, "IsReadOnlyMode") or call(app.ActiveView.Block, "IsReadOnly"):
            raise PadsError("document_read_only", "Test schematic is read-only")
        before = assignments(app, design)
        selected = read_selected(app, design, request["pins"])
        if any(
            p["block_path"] != design or "\\" in p["instance_path"] for p in selected
        ):
            raise PadsError(
                "packaging_scope_denied",
                "Only an unambiguous root-design placement is supported",
            )
        output = Path(request["output_dir"])
        phase = output / (
            "preview" if request["action"] == "packaging_preview" else "execute"
        )
        graph, evidence = export_round(
            database, design, phase / "candidate", _candidate_only=True
        )
        provenance = {
            "format": FORMAT,
            "snapshot": "DxD",
            "flow": "DX",
            "exporter_sha256": {
                name: fingerprint(
                    Path(os.environ["SDD_HOME"]) / "wv/win32/bin" / (name + ".exe")
                )
                for name in ("SctNetlist", "AllegroNetlist")
            },
        }
        if (
            provenance != request["baseline_provenance"]
            or graph != request["expected_graph"]
        ):
            raise PadsError(
                "packaging_graph_changed",
                "Candidate must equal the baseline graph plus only the new unconnected component",
            )
        resolve_membership(graph, selected)
        sources = {
            "protected_files": protected,
            "assignments": before,
            "packager_sha256": fingerprint(
                Path(os.environ["SDD_HOME"]) / "wg/win32/bin/package.exe"
            ),
            "database": str(database),
            "library": str(library),
        }
        if (
            request["action"] == "packaging_execute"
            and sources != request["expected_preview"]["sources"]
        ):
            raise PadsError(
                "stale_preview",
                "Project assignments, source files or native program changed",
            )
        if identity() != initial:
            raise PadsError(
                "target_changed", "Target changed before packaging preflight"
            )
        auxiliary = True
        proposal = run_package(project, design, phase / "proposal", True)
        # Preflight may write reports but may not change native identities/sheet/protected sources.
        if (
            protected_files(root) != protected
            or read_sheet(app, design, sheet) != current
            or identity() != initial
            or Path(call(app, "GetProjectData").iCDBDir).resolve() != database
            or Path(call(app, "GetProjectData").CentralLibraryPath).resolve() != library
        ):
            raise PadsError(
                "packaging_preflight_changed", "Preflight changed protected state"
            )
        stable_graph, stable_evidence = export_round(
            database, design, phase / "candidate_after", _candidate_only=True
        )
        if stable_graph != graph or {
            k: v["executable_sha256"] for k, v in stable_evidence.items()
        } != {k: v["executable_sha256"] for k, v in evidence.items()}:
            raise PadsError(
                "packaging_preflight_changed",
                "Candidate graph or exporter changed during preflight",
            )
        if request["action"] == "packaging_preview":
            if assignments(app, design) != before:
                raise PadsError(
                    "packaging_preflight_changed", "Preflight changed live assignments"
                )
            return {
                "ok": True,
                "sources": sources,
                "proposal": proposal,
                "candidate_graph": graph,
                "candidate_export_evidence": evidence,
                "candidate_is_packaging_preflight_only": True,
                "candidate_format": "SCT only; unpackaged Allegro fallback is not a verified logical graph",
                "resolved_new_pins": selected,
                "identity": initial,
                "project_write_attempted": True,
                "auxiliary_files_written": True,
                "schematic_update_attempted": False,
            }
        if time.time() > request["expires_at"]:
            raise PadsError(
                "preview_expired", "Packaging preview expired before database update"
            )
        if proposal["executable_sha256"] != sources["packager_sha256"]:
            raise PadsError(
                "packaging_source_changed", "Native packager changed after preflight"
            )
        if assignments(app, design) != before or identity() != initial:
            raise PadsError(
                "stale_preview", "Assignments changed before database update"
            )
        update = True
        applied = run_package(project, design, phase / "applied", False)
        after, after_evidence = export_round(database, design, phase / "verified")
        resolved = resolve_membership(
            after, read_selected(app, design, request["pins"])
        )
        if (
            after != graph
            or assignments(app, design) != before
            or read_sheet(app, design, sheet) != current
            or protected_files(root) != protected
            or identity() != initial
            or Path(call(app, "GetProjectData").iCDBDir).resolve() != database
            or Path(call(app, "GetProjectData").CentralLibraryPath).resolve() != library
        ):
            raise PadsError(
                "packaging_readback_mismatch",
                "Packaged graph, assignments, sheet or protected sources differ; inspect copy",
            )
        if (
            applied["executable_sha256"] != sources["packager_sha256"]
            or {k: v["executable_sha256"] for k, v in after_evidence.items()}
            != provenance["exporter_sha256"]
        ):
            raise PadsError("packaging_source_changed", "Native programs changed")
        return {
            "ok": True,
            "applied": applied,
            "verified_graph": after,
            "export_evidence": after_evidence,
            "resolved_new_pins": resolved,
            "component_assignments_unchanged": True,
            "exported_pin_membership_matches_preview": True,
            "full_design_live_pin_mapping_verified": False,
            "sheet_unchanged": True,
            "protected_files_unchanged": len(protected),
            "project_write_attempted": True,
            "auxiliary_files_written": True,
            "schematic_update_attempted": True,
            "identity": initial,
            "full_erc_verified": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "kind": (
                    exc.kind
                    if isinstance(exc, PadsError)
                    else "native_packaging_failed"
                ),
                "message": (
                    str(exc)
                    if isinstance(exc, PadsError)
                    else "Native packaging failed; inspect retained copy"
                ),
                "exception_type": type(exc).__name__,
            },
            "project_write_attempted": auxiliary or update,
            "auxiliary_files_written": auxiliary,
            "schematic_update_attempted": update,
            "schematic_update_outcome_unknown": isinstance(exc, PadsError)
            and exc.kind
            in (
                "packaging_unexpected_write",
                "packaging_preflight_changed",
                "packaging_outcome_unknown",
            ),
        }
