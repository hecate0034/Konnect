"""P2c real MCP routes, named-net extension, native checks and recovery acceptance."""

import argparse
import json
from pathlib import Path
import time
import uuid
from smoke_sessions import Client
from runtime.diagnostics import fingerprint


def run(server, python, sdd, state, fixture):
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    evidence = state / "evidence" / ("p2c-" + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
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
    start = time.monotonic()

    def invoke(step, tool, args, error=None):
        response = client.request("tools/call", {"name": tool, "arguments": args})
        value = json.loads(response["content"][0]["text"])
        (evidence / (step + ".json")).write_text(
            json.dumps(value, indent=2), encoding="utf-8"
        )
        report["steps"].append(
            {"step": step, "seconds": round(time.monotonic() - start, 2)}
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        print(step, flush=True)
        if error:
            assert (
                response.get("isError") and value["error"]["kind"] == error
            ), value.get("error")
        else:
            assert not response.get("isError"), value.get("error")
        return value

    def bind(label):
        return invoke(
            label,
            "bind_pads_session",
            {"domain": "designer", "expected_project": str(project)},
        )["session_id"]

    design, sheet = "CORPORATE", "Powersupply"

    def preview(label, op, error=None):
        return invoke(
            label,
            "preview_pads_schematic_change",
            {"session_id": sid, "design": design, "sheet": sheet, "operation": op},
            error,
        )

    def execute(label, p):
        return invoke(
            label, "execute_pads_schematic_change", {"preview_id": p["preview_id"]}
        )

    def check(label, baseline=None):
        args = {"session_id": sid, "design": design, "sheet": sheet}
        if baseline:
            args["baseline_id"] = baseline["check_id"]
        return invoke(label, "check_pads_schematic", args)

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

    try:
        init = client.request("initialize")
        report["server_version"] = init["serverInfo"]["version"]
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        sid = bind("bind")
        baseline = invoke(
            "baseline",
            "inspect_pads_sheet",
            {"session_id": sid, "design": design, "sheet": sheet},
        )["sheet"]
        first = check("baseline_check")
        source = next(
            c["target"] for c in baseline["components"] if c["target"]["refdes"] == "C2"
        )
        placed = []
        for ref, x, y in [("C900", 300, 200), ("C901", 600, 500), ("C902", 800, 200)]:
            p = preview(
                ref + "_preview",
                {
                    "kind": "place",
                    "source": source,
                    "refdes": ref,
                    "position": {"x": x, "y": y, "unit": "0.01_inch"},
                },
            )
            r = execute(ref + "_execute", p)
            target = next(
                c["target"]
                for c in r["sheet"]["components"]
                if c["target"]["refdes"] == ref
            )
            placed.append((p, target))
        loose = check("unconnected_positive_control", first)
        assert (
            len(loose["connectivity"]["unconnected_pins"])
            == len(first["connectivity"]["unconnected_pins"]) + 6
        )
        assert (
            loose["native_verify"]["counts"]["errors"]
            > first["native_verify"]["counts"]["errors"]
        )
        a = {"target": placed[0][1], "pin_uid": "$1P2"}
        b = {"target": placed[1][1], "pin_uid": "$1P1"}
        op = {
            "kind": "route",
            "a": a,
            "b": b,
            "unit": "0.01_inch",
            "points": [[300, 300], [300, 400], [600, 400], [600, 500]],
        }
        preview(
            "diagonal_refused",
            {**op, "points": [[300, 300], [600, 500]]},
            "invalid_route",
        )
        route = preview("route_preview", op)
        wired = execute("route_execute", route)
        new = next(
            n
            for n in wired["sheet"]["nets"]
            if n["uid"] not in {n["uid"] for n in baseline["nets"]}
        )
        assert len(new["segments"]) == 3
        third = {"target": placed[2][1], "pin_uid": "$1P2"}
        branch_op = {
            "kind": "route",
            "a": third,
            "b": {"net_uid": new["uid"], "net_name": new["name"], "point": [600, 400]},
            "unit": "0.01_inch",
            "points": [[800, 300], [800, 400], [600, 400]],
        }
        branch = preview("branch_preview", branch_op)
        execute("branch_execute", branch)
        sid = reopen("branch_reopen", branch)
        branched = check("branch_check", loose)
        matched = next(
            n for n in branched["connectivity"]["nets"] if n["uid"] == new["uid"]
        )
        assert len(matched["pins"]) == 3
        invoke(
            "route_recovery_with_branch_refused",
            "preview_pads_schematic_recovery",
            {"session_id": sid, "change_id": route["preview_id"]},
            "stale_recovery",
        )
        undo, _ = recover("branch_recovery", branch)
        named = next(
            n for n in baseline["nets"] if n["uid"] == "$11N3" and n["name"] == "VDD"
        )
        named_op = {
            "kind": "route",
            "a": third,
            "b": {
                "net_uid": named["uid"],
                "net_name": named["name"],
                "point": [895, 1025],
            },
            "unit": "0.01_inch",
            "points": [[800, 300], [800, 350], [700, 350], [700, 1025], [895, 1025]],
        }
        preview(
            "tangent_pin_exit_refused",
            {**named_op, "points": [[800, 300], [700, 300], [700, 1025], [895, 1025]]},
            "pin_exit_unsupported",
        )
        named_change = preview("named_net_preview", named_op)
        execute("named_net_execute", named_change)
        sid = reopen("named_net_reopen", named_change)
        named_check = check("named_net_check", loose)
        original_count = len(
            next(n for n in first["connectivity"]["nets"] if n["uid"] == named["uid"])[
                "pins"
            ]
        )
        assert (
            len(
                next(
                    n
                    for n in named_check["connectivity"]["nets"]
                    if n["uid"] == named["uid"]
                )["pins"]
            )
            == original_count + 1
        )
        recover("named_net_recovery", named_change)
        recover("route_recovery", route)
        for p, target in reversed(placed):
            undo, restored = recover(target["refdes"] + "_recovery", p)
        assert restored["sheet"] == baseline
        sid = reopen("baseline_reopen", undo)
        final = check("final_check", first)
        assert (
            final["comparison"]["netlist_equal"]
            and final["comparison"]["native_findings_equal"]
        )
        assert all(v == 0 for v in final["comparison"]["native_counts_delta"].values())
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
            or p.replace("\\", "/").endswith("/CORPORATE.pcb")
            or p.replace("\\", "/").endswith("/Verify.ini")
        }
        assert all(fingerprint(fixture / p) == h for p, h in protected.items())
        report.update(
            status="passed",
            requests=client.number,
            seconds=round(time.monotonic() - start, 2),
            source_files_unchanged=len(manifest["files"]),
            copied_protected_files_unchanged=len(protected),
            baseline_components=len(baseline["components"]),
            baseline_nets=len(baseline["nets"]),
            native_baseline=first["native_verify"]["counts"],
            native_positive_control=loose["native_verify"]["counts"],
            native_branched=branched["native_verify"]["counts"],
            native_named_net=named_check["native_verify"]["counts"],
            native_final=final["native_verify"]["counts"],
            complete_recorded_sheet_restored=True,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2c-acceptance-designer.json").write_text(
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
    p = argparse.ArgumentParser()
    for name in ("server", "python", "sdd", "state", "fixture"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    print(
        json.dumps(
            run(
                *(
                    getattr(a, n).resolve()
                    for n in ("server", "python", "sdd", "state", "fixture")
                )
            )
        )
    )
