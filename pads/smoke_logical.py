"""P2e actual MCP acceptance: globals, repeated hierarchy and fresh export deltas."""

import argparse
import json
from pathlib import Path
import time
import uuid

from smoke_sessions import Client
from runtime.diagnostics import fingerprint


def run(server, python, sdd, state, fixture):
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    evidence = state / "evidence" / ("p2e-" + uuid.uuid4().hex[:8])
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
        pins = [
            pin(r, p)
            for r, p in [
                ("U27", "15"),
                ("U29", "15"),
                ("U27", "13"),
                ("U9", "64"),
                ("U13", "AD8"),
                ("C2", "2"),
                ("U27", "8"),
                ("C10", "1"),
                ("U27", "16"),
                ("U25", "6"),
            ]
        ]
        original = logical("original_graph", pins)
        assert (
            find(original, "C2", "2")["logical_net_name"]
            == find(original, "U27", "8")["logical_net_name"]
            == "GND"
        )
        assert (
            find(original, "C10", "1")["logical_net_name"]
            == find(original, "U27", "16")["logical_net_name"]
            == "VDD"
        )
        assert {
            find(original, r, p)["logical_net_name"]
            for r, p in [("U9", "64"), ("U27", "13"), ("U13", "AD8")]
        } == {"U9LO1"}
        a, b = find(original, "U27", "15"), find(original, "U29", "15")
        assert all(
            a[k] == b[k]
            for k in (
                "component_uid",
                "pin_uid",
                "physical_net_uid",
                "physical_net_name",
                "sheet",
            )
        )
        assert (
            a["instance_path"] != b["instance_path"]
            and a["logical_net_name"] != b["logical_net_name"]
        )
        logical(
            "missing_pin_refused", [pin("R9999", "1")], error="logical_pin_not_unique"
        )
        logical(
            "shared_unit_pin_refused", [pin("U6", "8")], error="logical_pin_not_unique"
        )
        logical(
            "different_pin_baseline_refused",
            pins[:-1],
            original,
            "check_scope_mismatch",
        )
        logical(
            "implicit_number_refused", [pin("U12", "4")], error="logical_pin_not_unique"
        )
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
            "unpackaged_export_refused",
            pins + [pin("C900", "1")],
            error="logical_design_unpacked",
        )
        undo, restored_sheet = recover("place_recovery", placement)
        assert restored_sheet["sheet"] == baseline
        restored(logical("packaged_graph_restored", pins, original))
        sheet = "FPGA2"
        fpga = invoke(
            "fpga_baseline",
            "inspect_pads_sheet",
            {"session_id": sid, "design": "CORPORATE", "sheet": sheet},
        )["sheet"]
        target = next(
            c["target"] for c in fpga["components"] if c["target"]["refdes"] == "U25"
        )
        assert find(original, "U25", "6")["mapping_status"] == "unconnected"
        route = preview(
            "route_preview",
            {
                "kind": "route",
                "a": {"target": target, "pin_uid": "$1P12"},
                "b": {"net_uid": "$9N322", "net_name": "GND", "point": [495, 1240]},
                "points": [[1414, 1170], [1414, 1370], [495, 1370], [495, 1240]],
                "unit": "0.01_inch",
            },
        )
        execute("route_execute", route)
        connected = logical("connected_graph", pins, original)
        assert find(connected, "U25", "6")["logical_net_name"] == "GND"
        assert (
            not connected["comparison"]["netlist_equal"]
            and connected["comparison"]["components_equal"]
        )
        assert (
            len(connected["comparison"]["nets_before_only"])
            == len(connected["comparison"]["nets_after_only"])
            == 1
        )
        assert connected["comparison"]["nets_after_only"][0]["name"] == "GND"
        sid = reopen("connected_reopen", route)
        restored(logical("reopened_graph", pins, connected))
        undo, recovered = recover("route_recovery", route)
        assert recovered["sheet"] == fpga
        sid = reopen("recovery_reopen", undo)
        final = logical("final_graph", pins, original)
        restored(final)
        final_sheet = invoke(
            "final_powersupply",
            "inspect_pads_sheet",
            {"session_id": sid, "design": "CORPORATE", "sheet": "Powersupply"},
        )["sheet"]
        assert final_sheet == baseline
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
            shared_local_uid_isolation_verified=True,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2e-acceptance-designer.json").write_text(
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
