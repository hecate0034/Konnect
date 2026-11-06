"""P2f actual MCP acceptance: packaging, physical pins, reopen and recovery."""

import argparse
import json
from pathlib import Path
import time
import uuid

from smoke_sessions import Client
from runtime.diagnostics import fingerprint


def run(server, python, sdd, state, fixture):
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    evidence = state / "evidence" / ("p2f-" + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    start = time.monotonic()
    report = {
        "status": "running",
        "project": str(project),
        "evidence": str(evidence),
        "steps": [],
    }
    client = Client(
        [
            str(server),
            str(python),
            str(Path(__file__).with_name("worker.py")),
            str(sdd),
            str(state),
        ],
        timeout=300,
    )

    def invoke(label, tool, arguments, error=None):
        r = client.request("tools/call", {"name": tool, "arguments": arguments})
        value = json.loads(r["content"][0]["text"])
        (evidence / (label + ".json")).write_text(
            json.dumps(value, indent=2), encoding="utf-8"
        )
        report["steps"].append(
            {"step": label, "seconds": round(time.monotonic() - start, 2)}
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        print(label, flush=True)
        if error:
            assert r.get("isError") and value["error"]["kind"] == error, value
        else:
            assert not r.get("isError"), value.get("error")
        return value

    def bind(label):
        return invoke(
            label,
            "bind_pads_session",
            {"domain": "designer", "expected_project": str(project)},
        )["session_id"]

    def logical(label, pins, baseline=None, error=None):
        args = {"session_id": sid, "design": "CORPORATE", "pins": pins}
        if baseline:
            args["baseline_id"] = baseline["check_id"]
        return invoke(label, "inspect_pads_logical_connectivity", args, error)

    def preview(label, op):
        return invoke(
            label,
            "preview_pads_schematic_change",
            {
                "session_id": sid,
                "design": "CORPORATE",
                "sheet": sheet,
                "operation": op,
            },
        )

    def execute(label, p):
        return invoke(
            label, "execute_pads_schematic_change", {"preview_id": p["preview_id"]}
        )

    def reopen(label, p):
        r = invoke(
            label,
            "reopen_pads_schematic_change",
            {"session_id": sid, "change_id": p["preview_id"]},
        )
        assert r["status"] == "persistence_verified"
        return bind(label + "_bind")

    def recover(label, p):
        undo = invoke(
            label + "_preview",
            "preview_pads_schematic_recovery",
            {"session_id": sid, "change_id": p["preview_id"]},
        )
        return undo, execute(label + "_execute", undo)

    def pin(refdes, number):
        return {"refdes": refdes, "pin_number": number}

    def find(result, refdes, number):
        return next(
            p
            for p in result["resolved_pins"]
            if (p["refdes"], p["pin_number"]) == (refdes, number)
        )

    def restored(result):
        assert (
            result["comparison"]["netlist_equal"]
            and result["comparison"]["selected_pins_equal"]
        )

    try:
        report["server_version"] = client.request("initialize")["serverInfo"]["version"]
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        sid = bind("bind")
        sheet = "Powersupply"
        baseline = invoke(
            "baseline_sheet",
            "inspect_pads_sheet",
            {"session_id": sid, "design": "CORPORATE", "sheet": "Powersupply"},
        )["sheet"]
        pins = [pin("C2", "1"), pin("C2", "2"), pin("U27", "15"), pin("U29", "15")]
        original = logical("original_graph", pins)
        source = next(
            c["target"] for c in baseline["components"] if c["target"]["refdes"] == "C2"
        )
        placement = preview(
            "place_preview",
            {
                "kind": "place",
                "source": source,
                "refdes": "C900",
                "position": {"x": 300, "y": 200, "unit": "0.01_inch"},
            },
        )
        execute("place_execute", placement)
        logical(
            "unpackaged_refused", [pin("C900", "1")], error="logical_design_unpacked"
        )
        package = invoke(
            "package_preview",
            "preview_pads_packaging",
            {
                "session_id": sid,
                "change_id": placement["preview_id"],
                "baseline_id": original["check_id"],
            },
        )
        assert package["preview"]["proposal"]["bam_bytes"] == 0
        assert (
            package["preview"]["auxiliary_files_written"]
            and not package["preview"]["schematic_update_attempted"]
        )
        packed = invoke(
            "package_execute",
            "execute_pads_packaging",
            {"preview_id": package["preview_id"]},
        )
        assert packed["component_assignments_unchanged"] and packed["sheet_unchanged"]
        assert packed["verified_graph"] == package["preview"]["candidate_graph"]
        invoke(
            "package_replay_refused",
            "execute_pads_packaging",
            {"preview_id": package["preview_id"]},
            "preview_not_ready",
        )
        connected = logical("packaged_graph", pins, original)
        assert (
            connected["comparison"]["selected_pins_equal"]
            and not connected["comparison"]["components_equal"]
        )
        assert connected["netlist"]["nets"] == original["netlist"]["nets"]
        assert (
            len(connected["netlist"]["components"])
            == len(original["netlist"]["components"]) + 1
        )
        new_pins = logical("new_physical_pins", [pin("C900", "1"), pin("C900", "2")])
        assert all(
            p["mapping_status"] == "unconnected" for p in new_pins["resolved_pins"]
        )
        sid = reopen("packaged_reopen", placement)
        restored(logical("reopened_graph", pins, connected))
        restored(
            logical("reopened_new_pins", [pin("C900", "1"), pin("C900", "2")], new_pins)
        )
        undo, recovered = recover("placement_recovery", placement)
        assert recovered["sheet"] == baseline
        sid = reopen("recovery_reopen", undo)
        final = logical("final_graph", pins, original)
        restored(final)
        invoke(
            "recovered_placement_refused",
            "preview_pads_packaging",
            {
                "session_id": sid,
                "change_id": placement["preview_id"],
                "baseline_id": original["check_id"],
            },
            "packaging_scope_denied",
        )
        invoke("release", "release_pads_session", {"session_id": sid})
        manifest = json.loads((fixture / "source-manifest.json").read_text())
        assert all(
            fingerprint(Path(manifest["source"]) / p) == h
            for p, h in manifest["files"].items()
        )
        protected = {
            p: h
            for p, h in manifest["files"].items()
            if Path(p).parts[0] == "Library"
            or p.replace("\\", "/").endswith(("/CORPORATE.pcb", "/Verify.ini"))
        }
        assert all(fingerprint(fixture / p) == h for p, h in protected.items())
        report.update(
            status="passed",
            requests=client.number,
            seconds=round(time.monotonic() - start, 2),
            exported_components=len(original["netlist"]["components"]),
            exported_nets=len(original["netlist"]["nets"]),
            connected_exported_pins=sum(
                len(n["pins"]) for n in original["netlist"]["nets"]
            ),
            selected_baseline_pins=len(pins),
            source_files_unchanged=len(manifest["files"]),
            copied_protected_files_unchanged=len(protected),
            complete_recorded_sheet_restored=True,
            complete_exported_graph_restored=True,
            identity_preserving_packaging_verified=True,
            explicit_new_pin_mapping_verified=True,
            complete_semantic_recovery_verified=True,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2f-acceptance-designer.json").write_text(
            json.dumps(report, indent=2)
        )
        return report
    except Exception as exc:
        report.update(status="failed", failure=str(exc), requests=client.number)
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        raise
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("server", "python", "sdd", "state", "fixture"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                *(
                    getattr(args, n).resolve()
                    for n in ("server", "python", "sdd", "state", "fixture")
                )
            )
        )
    )
