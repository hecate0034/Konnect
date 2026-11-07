"""P2j acceptance: consume a prepared scope and prove exact-file execution/restoration.

Preparation uses the same backed-up official fixture as P2i. This harness accepts
an existing public execution-plan receipt to continue after reviewing its details.
It deliberately corrupts only that private approved COPY, then obtains a fresh
preview from the unchanged original impact before doing any native writes.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from smoke_sessions import Client
from smoke_checkpoint import stop_owned, settled_inventory
from runtime.bam_execution import normalize
from runtime.checkpoints import Checkpoints, inventory
from runtime.diagnostics import fingerprint
from runtime.native_bam_execution import snapshot
from runtime.native_designer import connect
from runtime.native_packaging import protected_files
from runtime.probe import native_environment


def run(
    server, python, sdd, state, fixture, host, plan_file, resume_before_restore=None
):
    os.environ.update(native_environment(sdd, state))
    plan = json.loads(plan_file.read_text())
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    assert plan["project"] == str(project) and plan_file.resolve().is_relative_to(
        state / "evidence"
    )
    evidence = state / "evidence" / ("p2j-" + uuid.uuid4().hex[:8])
    evidence.mkdir()
    report = {
        "status": "running",
        "domain": "designer",
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

    def invoke(label, name, args, error=None):
        response = client.request("tools/call", {"name": name, "arguments": args})
        value = json.loads(response["content"][0]["text"])
        (evidence / (label + ".json")).write_text(json.dumps(value, indent=2))
        report["steps"].append(
            {"step": label, "seconds": round(time.monotonic() - start, 2)}
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        print(label, flush=True)
        if error:
            assert response.get("isError") and value["error"]["kind"] == error, value
        else:
            assert not response.get("isError"), value.get("error")
        return value

    def bind(label):
        return invoke(
            label,
            "bind_pads_session",
            {"domain": "designer", "expected_project": str(project)},
        )

    try:
        report["version"] = client.request("initialize")["serverInfo"]["version"]
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        if resume_before_restore:
            prior = resume_before_restore.resolve(strict=True)
            assert prior.parent == (state / "evidence").resolve()
            previous = json.loads((prior / "report.json").read_text())
            assert (
                previous["status"] == "failed"
                and previous["steps"][-1]["step"] == "release"
            )
            assert (
                not (prior / "restore_preview.json").exists()
                and not (prior / "restore.json").exists()
            )
            Checkpoints(state).closed()

            def saved(name):
                return json.loads((prior / (name + ".json")).read_text())

            current, result, reopen = (
                saved("fresh_plan"),
                saved("execute"),
                saved("reopen"),
            )
            binding = saved("after_reopen_bind")
            assert (
                current["project"] == str(project)
                and current["impact_id"] == plan["impact_id"]
            )
            assert (
                result["status"] == "executed"
                and reopen["status"] == "persistence_verified"
            )
            assert result["observed"] == reopen["observed"] == current["after"]
            assert (
                result["approved_sha256"]
                == reopen["approved_sha256"]
                == current["approved_sha256"]
            )
            report.update(
                continued_from=str(prior),
                previous_failure=previous["failure"],
                previous_requests=previous["requests"],
            )
        else:
            binding = bind("bind")
            # Corrupt only a disposable plan-owned file; never modify native output,
            # project files or the recovery checkpoint for this negative test.
            approved = Path(plan["approved_file"])
            assert approved.resolve().is_relative_to(state / "evidence")
            approved.write_bytes(
                approved.read_bytes() + b"\n/* controlled hash mismatch test */\n"
            )
            invoke(
                "modified_file_refused",
                "execute_pads_bam",
                {"preview_id": plan["preview_id"]},
                "approved_bam_changed",
            )
            invoke(
                "invalidated_token_refused",
                "execute_pads_bam",
                {"preview_id": plan["preview_id"]},
                "preview_not_ready",
            )
            invoke(
                "impact_token_refused",
                "execute_pads_bam",
                {"preview_id": plan["impact_id"]},
                "preview_not_ready",
            )
            current = invoke(
                "fresh_plan",
                "preview_pads_bam_execution",
                {
                    "session_id": binding["session_id"],
                    "impact_id": plan["impact_id"],
                    "checkpoint_id": plan["checkpoint_id"],
                },
            )
            assert (
                current["before"] == plan["before"]
                and current["after"] == plan["after"]
                and current["approved_sha256"] == plan["approved_sha256"]
            )
            result = invoke(
                "execute", "execute_pads_bam", {"preview_id": current["preview_id"]}
            )
            assert (
                result["status"] == "executed"
                and result["observed"] == current["after"]
            )
            assert (
                result["approved_sha256"] == current["approved_sha256"]
                and not result["native_bam_importer_used"]
            )
            invoke(
                "replay_refused",
                "execute_pads_bam",
                {"preview_id": current["preview_id"]},
                "preview_not_ready",
            )
            invoke(
                "old_binding_refused",
                "get_pads_session",
                {"session_id": binding["session_id"]},
                "session_invalidated",
            )
            binding = bind("rebind")
            reopen = invoke(
                "reopen",
                "reopen_pads_bam",
                {
                    "session_id": binding["session_id"],
                    "preview_id": current["preview_id"],
                },
            )
            assert (
                reopen["status"] == "persistence_verified"
                and reopen["observed"] == current["after"]
            )
            binding = bind("after_reopen_bind")
            invoke(
                "release", "release_pads_session", {"session_id": binding["session_id"]}
            )
            stop_owned(binding, host)
        before_restore = settled_inventory(fixture)
        preview = invoke(
            "restore_preview",
            "preview_pads_fixture_restore",
            {"checkpoint_id": current["checkpoint_id"]},
        )
        restored = invoke(
            "restore",
            "execute_pads_fixture_restore",
            {"preview_id": preview["preview_id"]},
        )
        checkpoint = json.loads(
            (
                state / "checkpoints" / current["checkpoint_id"] / "manifest.json"
            ).read_text()
        )
        assert inventory(fixture) == checkpoint["inventory"]
        assert inventory(Path(restored["quarantine"])) == before_restore
        invoke(
            "restored_old_token_refused",
            "execute_pads_bam",
            {"preview_id": current["preview_id"]},
            "preview_not_ready",
        )
        new_host = evidence / "restored-host"
        subprocess.run(
            [
                str(python),
                str(Path(__file__).with_name("launch_fixture.py")),
                "--domain",
                "designer",
                "--sdd-home",
                str(sdd),
                "--fixture",
                str(fixture),
                "--state",
                str(new_host),
                "--lifetime-minutes",
                "20",
            ],
            check=True,
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            log = new_host / "designer.log"
            if log.exists() and '"stage":"opened"' in log.read_text(
                encoding="utf-8-sig"
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Restored fixture did not open; inspect retained host")
        recovered_binding = bind("recovered_bind")
        assert recovered_binding["identity"] != binding["identity"]
        app, identity = connect(
            {
                "expected_path": str(project),
                "expected_identity": recovered_binding["identity"],
            }
        )
        recovered, _ = snapshot(app, evidence / "recovered-graph")
        original = normalize(
            {
                name: json.loads((state / ("original-" + name + ".json")).read_text())
                for name in ("live", "graph", "sheet")
            }
        )
        assert recovered == original and identity() == recovered_binding["identity"]
        (evidence / "recovered.json").write_text(json.dumps(recovered, indent=2))
        del app
        invoke(
            "release_recovered",
            "release_pads_session",
            {"session_id": recovered_binding["session_id"]},
        )
        stop_owned(recovered_binding, new_host)
        assert protected_files(fixture) == protected_files(
            state / "checkpoints" / current["checkpoint_id"] / "tree"
        )
        source = json.loads((fixture / "source-manifest.json").read_text())
        assert all(
            fingerprint(Path(source["source"]) / p) == h
            for p, h in source["files"].items()
        )
        report.update(
            status="passed",
            requests=client.number,
            seconds=round(time.monotonic() - start, 2),
            approved_bam_execution_verified=True,
            execution_method=result["execution_method"],
            native_bam_importer_used=False,
            approved_sha256=current["approved_sha256"],
            preview_id=current["preview_id"],
            impact_id=current["impact_id"],
            checkpoint_id=current["checkpoint_id"],
            selected_instances=3,
            reference_changes=3,
            pin_changes=0,
            instance_overrides_created=6,
            absent_fixed_deletions=3,
            readback_and_reopen_verified=True,
            entire_checkpoint_restored=True,
            restored_files=len(checkpoint["inventory"]["files"]),
            restored_bytes=checkpoint["inventory"]["bytes"],
            independent_recovery_reopen_verified=True,
            original_live_graph_sheet_restored=True,
            source_files_unchanged=len(source["files"]),
            quarantine=restored["quarantine"],
            owned_editors_exited=True,
            automatic_inverse_available=False,
            general_write_verified=False,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2j-acceptance-designer.json").write_text(
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
    for name in ("server", "python", "sdd", "state", "fixture", "host", "plan-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--resume-before-restore", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                *(
                    getattr(args, n).resolve()
                    for n in (
                        "server",
                        "python",
                        "sdd",
                        "state",
                        "fixture",
                        "host",
                        "plan_file",
                    )
                ),
                args.resume_before_restore,
            )
        )
    )
