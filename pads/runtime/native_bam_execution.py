"""Native application of a hash-bound, explicitly supported BAM subset."""

import os
import json
from pathlib import Path
import time

from .bam_execution import (
    VERSION,
    TARGETS,
    SCOPE,
    normalize,
    planned,
    read_approved,
    require,
)
from .diagnostics import fingerprint
from .native_backannotation import read_state
from .native_designer import call
from .native_packaging import protected_files
from .logical import export_round
from .sessions import PadsError


def snapshot(app, output):
    from .native_schematic import read_sheet

    live, _ = read_state(app, "CORPORATE")
    sheet = read_sheet(app, "CORPORATE", "Powersupply")
    output.parent.mkdir(parents=True, exist_ok=True)
    (output.parent / (output.name + "-live-sheet.json")).write_text(
        json.dumps({"live": live, "sheet": sheet}, indent=2), encoding="utf-8"
    )
    graph, evidence = export_round(
        Path(call(app, "GetProjectData").iCDBDir), "CORPORATE", output
    )
    value = normalize({"live": live, "graph": graph, "sheet": sheet})
    (output.parent / (output.name + "-state.json")).write_text(
        json.dumps(value, indent=2), encoding="utf-8"
    )
    return value, {k: v["executable_sha256"] for k, v in evidence.items()}


def operate(app, request, identity, initial):
    attempted = lifecycle = False
    try:
        action = request["action"]
        output = Path(request["output_dir"])
        root = Path(request["policy"]["fixture"]).resolve(strict=True)
        records = read_approved(request["approved_file"], request["approved_sha256"])
        require(
            records == request["records"],
            "Parsed approved records differ",
            "approved_bam_changed",
        )
        before_protected = protected_files(root)
        current, exporters = snapshot(app, output / "before")
        executable = Path(initial["executable"])
        provenance = {
            "adapter": VERSION,
            "designer_sha256": fingerprint(executable),
            "exporters": exporters,
        }
        if action == "bam_plan":
            source = request["impact_provenance"]
            require(
                fingerprint(
                    Path(request["expected_path"]).parent
                    / "Integration/LocalPartsDB.pdb"
                )
                == source["pdb_sha256"]
                and fingerprint(
                    Path(os.environ["SDD_HOME"]) / "wg/win32/bin/package.exe"
                )
                == source["packager_sha256"]
                and exporters == source["exporters"],
                "Impact source/tool fingerprints changed",
                "stale_preview",
            )
            require(
                current["live"] == normalize(request["impact_live"])
                and current["graph"] == normalize(request["impact_graph"]),
                "Live state differs from impact preview",
                "stale_preview",
            )
            expected = planned(
                records, current["live"], current["graph"], current["sheet"]
            )
        else:
            require(
                provenance == request["provenance"]
                and before_protected == request["protected"],
                "Native tools or protected files changed",
                "stale_preview",
            )
            expected = request["after"]
            require(
                current == (request["before"] if action == "bam_apply" else expected),
                "Live, sheet or graph state changed",
                "stale_preview",
            )
            if action == "bam_apply":
                require(
                    planned(
                        records, current["live"], current["graph"], current["sheet"]
                    )
                    == expected,
                    "Plan no longer matches approved file",
                    "stale_preview",
                )
                require(
                    time.time() <= request["expires_at"],
                    "Execution preview expired",
                    "preview_expired",
                )
                require(
                    not call(app, "IsReadOnlyMode")
                    and not call(app.ActiveView.Block, "IsReadOnly"),
                    "Native project is read only",
                    "document_read_only",
                )
                # Parse the SAME bytes again at the last gate. Execute immutable
                # records in memory; never ask the packager to regenerate a BAM.
                require(
                    read_approved(request["approved_file"], request["approved_sha256"])
                    == records
                    and identity() == initial,
                    "Approved input or application changed",
                    "approved_bam_changed",
                )
                objects = {}
                for component in call(
                    app, "DesignComponents", "", "CORPORATE", "-1", "", True
                ):
                    if (
                        str(component.UID) in TARGETS
                        and str(component.Parent.SheetNum) == "Powersupply"
                        and str(call(component.Parent, "GetName", 0)) == "CORPORATE"
                    ):
                        require(
                            str(component.UID) not in objects,
                            "Ambiguous native instance",
                        )
                        objects[str(component.UID)] = component
                require(set(objects) == set(TARGETS), "Native instances missing")
                for record in records:
                    component = objects[record["uid"]]
                    require(
                        identity() == initial
                        and str(component.Refdes) == record["refdes"]["before"],
                        "Target changed before setter",
                        "target_changed",
                    )
                    attempted = True
                    for name, value in record["attributes"].items():
                        attribute = call(component, "FindAttribute", name)
                        require(
                            attribute is not None,
                            "Required attribute disappeared",
                            "target_changed",
                        )
                        attribute.InstanceValue = value
                    # PARAM 23 DELETE is an explicit verified no-op: target flag
                    # absent in both base and instance. Do not delete shared data.
                    component.Refdes = record["refdes"]["after"]
            elif action == "bam_reopen":
                lifecycle = True
                call(app, "CloseProject")
                call(app, "OpenProject", request["expected_path"])
                from .native_schematic import existing_sheet

                existing_sheet(app, "CORPORATE", "Powersupply")
            else:
                require(action == "bam_verify", "Unsupported action")
        if action == "bam_plan":
            actual = current
        else:
            actual, after_exporters = snapshot(app, output / "after")
            require(
                actual == expected and after_exporters == exporters,
                "Native readback differs from approved result",
                "bam_readback_mismatch",
            )
        require(
            identity() == initial
            and protected_files(root) == before_protected
            and fingerprint(executable) == provenance["designer_sha256"],
            "Identity or protected source changed",
            "bam_readback_mismatch",
        )
        require(
            read_approved(request["approved_file"], request["approved_sha256"])
            == records,
            "Approved bytes changed during operation",
            "approved_bam_changed",
        )
        return {
            "ok": True,
            "before": current,
            "after": expected,
            "observed": actual,
            "protected": before_protected,
            "provenance": provenance,
            "approved_sha256": request["approved_sha256"],
            "identity": initial,
            "project_write_attempted": attempted,
            "lifecycle_attempted": lifecycle,
            "native_bam_importer_used": False,
            "execution_method": VERSION,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "kind": exc.kind if isinstance(exc, PadsError) else "native_bam_failed",
                "message": str(exc),
            },
            "project_write_attempted": attempted,
            "lifecycle_attempted": lifecycle,
        }
