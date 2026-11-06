"""Real MCP geometry, placement, wiring and reverse-order recovery acceptance."""

import argparse
import json
from pathlib import Path
import time
import uuid
from smoke_sessions import Client
from runtime.diagnostics import fingerprint


def run(server, python, sdd, state, fixture):
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    policy = json.loads((state / "fixture-edits.json").read_text())
    assert Path(policy["project"]).resolve() == project
    evidence = state / "evidence" / ("p2b-" + uuid.uuid4().hex[:8])
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

    def record(name, result):
        (evidence / (name + ".json")).write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        report["steps"].append(
            {"step": name, "seconds": round(time.monotonic() - start, 2)}
        )
        (evidence / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(name, flush=True)
        return result

    def invoke(step, tool, args, error=None):
        response = client.request("tools/call", {"name": tool, "arguments": args})
        result = record(step, json.loads(response["content"][0]["text"]))
        if error:
            assert (
                response.get("isError") and result.get("error", {}).get("kind") == error
            ), result.get("error")
        else:
            assert not response.get("isError"), result.get("error")
        return result

    def bind(step):
        return invoke(
            step,
            "bind_pads_session",
            {"domain": "designer", "expected_project": str(project)},
        )["session_id"]

    design, sheet = "CORPORATE", "Powersupply"

    def preview(step, sid, op):
        return invoke(
            step,
            "preview_pads_schematic_change",
            {"session_id": sid, "design": design, "sheet": sheet, "operation": op},
        )

    def execute(step, p):
        return invoke(
            step, "execute_pads_schematic_change", {"preview_id": p["preview_id"]}
        )

    def reopen(step, sid, p):
        result = invoke(
            step,
            "reopen_pads_schematic_change",
            {"session_id": sid, "change_id": p["preview_id"]},
        )
        assert result["status"] == "persistence_verified"
        return bind(step + "_bind")

    def recovery(step, sid, p):
        return invoke(
            step,
            "preview_pads_schematic_recovery",
            {"session_id": sid, "change_id": p["preview_id"]},
        )

    try:
        client.request("initialize")
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        sid = bind("bind")
        baseline = invoke(
            "baseline",
            "inspect_pads_sheet",
            {"session_id": sid, "design": design, "sheet": sheet},
        )["sheet"]
        source = next(
            c for c in baseline["components"] if c["target"]["refdes"] == "C2"
        )
        move = {
            "kind": "edit",
            "target": source["target"],
            "changes": {
                "attribute": {"name": "Description", "value": "P2B geometry test"},
                "position": {**source["position"], "y": source["position"]["y"] + 10},
            },
        }
        moved = preview("move_preview", sid, move)
        stale = preview("stale_move_preview", sid, move)
        execute("move_execute", moved)
        invoke(
            "move_replay_refused",
            "execute_pads_schematic_change",
            {"preview_id": moved["preview_id"]},
            "preview_not_ready",
        )
        invoke(
            "stale_sheet_refused",
            "execute_pads_schematic_change",
            {"preview_id": stale["preview_id"]},
            "stale_preview",
        )
        sid = reopen("move_reopen", sid, moved)
        undo = recovery("move_recovery_preview", sid, moved)
        restored = execute("move_recovery_execute", undo)
        assert restored["sheet"] == baseline
        sid = reopen("move_recovery_reopen", sid, undo)
        placed = []
        for n, y in enumerate((200, 500)):
            p = preview(
                f"place_{n}_preview",
                sid,
                {
                    "kind": "place",
                    "source": source["target"],
                    "refdes": f"C90{n}",
                    "position": {"x": 300, "y": y, "unit": "0.01_inch"},
                },
            )
            result = execute(f"place_{n}_execute", p)
            target = next(
                c["target"]
                for c in result["sheet"]["components"]
                if c["target"]["refdes"] == f"C90{n}"
            )
            placed.append((p, target))
        # Removing the first placement now would overwrite a later change: refuse it.
        invoke(
            "later_change_recovery_refused",
            "preview_pads_schematic_recovery",
            {"session_id": sid, "change_id": placed[0][0]["preview_id"]},
            "stale_recovery",
        )
        connect = {
            "kind": "connect",
            "a": {"target": placed[0][1], "pin_uid": "$1P2"},
            "b": {"target": placed[1][1], "pin_uid": "$1P1"},
        }
        wire = preview("wire_preview", sid, connect)
        connected = execute("wire_execute", wire)
        readback = invoke(
            "wire_independent_readback",
            "inspect_pads_sheet",
            {"session_id": sid, "design": design, "sheet": sheet},
        )
        assert (
            readback["sheet"] == connected["sheet"] and not readback["geometry_issues"]
        )
        sid = reopen("wire_reopen", sid, wire)
        undo_wire = recovery("wire_recovery_preview", sid, wire)
        execute("wire_recovery_execute", undo_wire)
        for n in (1, 0):
            undo_place = recovery(f"place_{n}_recovery_preview", sid, placed[n][0])
            restored = execute(f"place_{n}_recovery_execute", undo_place)
        assert restored["sheet"] == baseline
        sid = reopen("baseline_reopen", sid, undo_place)
        final = invoke(
            "final_read",
            "inspect_pads_sheet",
            {"session_id": sid, "design": design, "sheet": sheet},
        )
        assert final["sheet"] == baseline and not final["geometry_issues"]
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
        }
        assert all(fingerprint(fixture / p) == h for p, h in protected.items())
        report.update(
            status="passed",
            requests=client.number,
            seconds=round(time.monotonic() - start, 2),
            baseline_components=len(baseline["components"]),
            baseline_nets=len(baseline["nets"]),
            source_files_unchanged=len(manifest["files"]),
            copied_protected_files_unchanged=len(protected),
            complete_recorded_sheet_restored=True,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2b-acceptance-designer.json").write_text(
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
    a = parser.parse_args()
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
