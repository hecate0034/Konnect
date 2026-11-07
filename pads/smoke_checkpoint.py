"""P2i acceptance on an explicitly prepared, fully checkpointed test fixture.

The developer prepares outside-page Frozen Package=Fix overrides only after a
closed checkpoint. Public preview tools never set those overrides. Baselines
original-live.json and original-graph.json must precede that preparation.
"""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from smoke_sessions import Client
from runtime.checkpoints import Checkpoints, inventory
from runtime.diagnostics import fingerprint
from runtime.native_designer import connect, call
from runtime.native_backannotation import read_state
from runtime.logical import export_round
from runtime.probe import native_environment
from runtime.sessions import PadsError


def settled_inventory(fixture):
    """Acceptance-only wait after an owned editor exits; never retry a restore."""
    deadline, previous = time.monotonic() + 20, None
    while time.monotonic() < deadline:
        try:
            current = inventory(fixture)
            if current == previous:
                return current
            previous = current
        except (FileNotFoundError, PermissionError, PadsError) as exc:
            if isinstance(exc, PadsError) and exc.kind != "checkpoint_source_changed":
                raise
            previous = None
        time.sleep(1)
    raise RuntimeError(
        "Fixture is still changing after editor exit; inspect before restore"
    )


def stop_owned(binding, host):
    expected = binding["identity"]
    app, identity = connect(
        {"expected_path": expected["project_path"], "expected_identity": expected}
    )
    assert identity() == expected
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, expected["pid"])
    assert handle and identity() == expected
    (host / "designer.control").write_text("quit", encoding="utf-8")
    del app
    try:
        assert (
            kernel.WaitForSingleObject(handle, 45000) == 0
        ), "Owned editor did not exit"
    finally:
        kernel.CloseHandle(handle)


def run(
    server, python, sdd, state, fixture, host, checkpoint_id, resume_before_restore=None
):
    os.environ.update(native_environment(sdd, state))
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    checkpoint = json.loads(
        (state / "checkpoints" / checkpoint_id / "manifest.json").read_text()
    )
    assert checkpoint["fixture"] == str(fixture)
    original_live = json.loads((state / "original-live.json").read_text())
    original_graph = json.loads((state / "original-graph.json").read_text())
    evidence = state / "evidence" / ("p2i-" + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    report = {
        "status": "running",
        "domain": "designer",
        "evidence": str(evidence),
        "checkpoint_id": checkpoint_id,
        "steps": [],
    }
    start = time.monotonic()
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
        report["server_version"] = client.request("initialize")["serverInfo"]["version"]
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        if resume_before_restore:
            prior = resume_before_restore.resolve(strict=True)
            assert prior.parent == (state / "evidence").resolve()
            previous = json.loads((prior / "report.json").read_text())
            assert (
                previous["status"] == "failed"
                and previous["checkpoint_id"] == checkpoint_id
            )
            assert previous["steps"][-1]["step"] == "release_before_restore"
            assert not (prior / "restore_execute.json").exists()
            Checkpoints(state).closed()
            report["continued_from"] = str(prior)
            report["previous_failure"] = previous["failure"]

            def saved(name):
                return json.loads((prior / (name + ".json")).read_text())

            binding, first, after = (
                saved("rebind"),
                saved("scoped_preview"),
                saved("scoped_after_reopen"),
            )
            assert binding["identity"]["project_path"] == str(project)
            assert (
                first["repeatability"]["identical"]
                and after["repeatability"]["identical"]
            )
            assert (
                all(after["comparison"].values())
                and first["proposal_records"] == after["proposal_records"]
            )
            edit = saved("edit_preview")
            assert saved("edit_execute")["component"] == edit["after"]
            component = saved("component_before")["component"]
            target = edit["target"]
            args = {
                "design": "CORPORATE",
                "mode": "repackage_unfixed",
                "scope_sheet": "Powersupply",
            }
        else:
            binding = bind("bind")
            sid = binding["session_id"]
            invoke(
                "checkpoint_while_open_refused",
                "create_pads_fixture_checkpoint",
                {},
                "checkpoint_editor_open",
            )
            invoke(
                "restore_while_open_refused",
                "preview_pads_fixture_restore",
                {"checkpoint_id": checkpoint_id},
                "checkpoint_editor_open",
            )
            args = {
                "session_id": sid,
                "design": "CORPORATE",
                "mode": "repackage_unfixed",
                "scope_sheet": "Powersupply",
            }
            first = invoke("scoped_preview", "preview_pads_backannotation", args)
            assert first["repeatability"]["identical"]
            assert len(first["proposal_records"]) == 3
            assert all(
                r["scope"] == "CORPORATE(Powersupply)" and not r["pins"]
                for r in first["proposal_records"]
            )
            assert (
                not first["executable_preview"]
                and not first["schematic_update_attempted"]
            )
            invoke(
                "impact_execution_refused",
                "execute_pads_packaging",
                {"preview_id": first["impact_id"]},
                "preview_not_ready",
            )
            invoke(
                "scope_baseline_refused",
                "preview_pads_backannotation",
                {
                    **args,
                    "scope_sheet": "control_buffers",
                    "baseline_id": first["impact_id"],
                },
                "check_scope_mismatch",
            )
            invoke("release_before_reopen", "release_pads_session", {"session_id": sid})
            subprocess.run(
                [
                    str(python),
                    str(Path(__file__).with_name("smoke_pin_mapping.py")),
                    "--reopen-binding",
                    str(evidence / "bind.json"),
                    "--fixture",
                    str(fixture),
                ],
                check=True,
                timeout=60,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            binding = bind("rebind")
            sid = binding["session_id"]
            after = invoke(
                "scoped_after_reopen",
                "preview_pads_backannotation",
                {**args, "session_id": sid, "baseline_id": first["impact_id"]},
            )
            assert after["repeatability"]["identical"] and all(
                after["comparison"].values()
            )
            assert after["proposal_records"] == first["proposal_records"]
            target = {
                "design": "CORPORATE",
                "sheet": "Powersupply",
                "uid": "$11I16",
                "path": "$11I16",
                "refdes": "C2",
            }
            inspect_args = {"session_id": sid, "target": target}
            component = invoke(
                "component_before", "inspect_pads_component", inspect_args
            )["component"]
            edit = invoke(
                "edit_preview",
                "preview_pads_component_edit",
                {
                    **inspect_args,
                    "changes": {
                        "attribute": {
                            "name": "Description",
                            "value": "P2i full checkpoint restoration probe",
                        }
                    },
                },
            )
            changed = invoke(
                "edit_execute",
                "execute_pads_component_edit",
                {"preview_id": edit["preview_id"]},
            )
            assert (
                changed["component"] == edit["after"]
                and changed["component"] != component
            )
            invoke(
                "release_before_restore", "release_pads_session", {"session_id": sid}
            )
            stop_owned(binding, host)
        current = settled_inventory(fixture)
        restore = invoke(
            "restore_preview",
            "preview_pads_fixture_restore",
            {"checkpoint_id": checkpoint_id},
        )
        assert restore["file_changes"]["replaced"]
        restored = invoke(
            "restore_execute",
            "execute_pads_fixture_restore",
            {"preview_id": restore["preview_id"]},
        )
        assert inventory(fixture) == checkpoint["inventory"]
        assert inventory(Path(restored["quarantine"])) == current
        invoke(
            "restore_replay_refused",
            "execute_pads_fixture_restore",
            {"preview_id": restore["preview_id"]},
            "preview_not_ready",
        )
        invoke(
            "old_edit_reopen_refused",
            "reopen_pads_edit",
            {"preview_id": edit["preview_id"]},
            "preview_not_ready",
        )
        fresh = invoke("checkpoint_closed", "create_pads_fixture_checkpoint", {})
        assert fresh["tree_digest"] == checkpoint["inventory"]["digest"]
        new_host = evidence / "host-restored"
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
            raise RuntimeError("Restored editor did not open; inspect host log")
        binding2 = bind("restored_bind")
        assert binding2["identity"] != binding["identity"]
        sid2 = binding2["session_id"]
        final = invoke(
            "component_after_restore",
            "inspect_pads_component",
            {"session_id": sid2, "target": target},
        )["component"]
        assert final == component
        invoke(
            "restored_scope_requires_freeze",
            "preview_pads_backannotation",
            {**args, "session_id": sid2},
            "backannotation_scope_not_frozen",
        )
        app, identity = connect(
            {"expected_path": str(project), "expected_identity": binding2["identity"]}
        )
        live, _ = read_state(app, "CORPORATE")
        graph, _ = export_round(
            Path(call(app, "GetProjectData").iCDBDir),
            "CORPORATE",
            evidence / "restored-graph",
        )
        assert (
            live == original_live
            and graph == original_graph
            and identity() == binding2["identity"]
        )
        (evidence / "restored-live.json").write_text(json.dumps(live, indent=2))
        del app
        invoke("release", "release_pads_session", {"session_id": sid2})
        stop_owned(binding2, new_host)
        manifest = json.loads((fixture / "source-manifest.json").read_text())
        assert all(
            fingerprint(Path(manifest["source"]) / p) == h
            for p, h in manifest["files"].items()
        )
        report.update(
            status="passed",
            requests=client.number,
            seconds=round(time.monotonic() - start, 2),
            scoped_backannotation_and_checkpoint_verified=True,
            scoped_instances=3,
            native_runs_compared=4,
            candidate_equal_after_reopen=True,
            summary=first["impact"]["summary"],
            restored_files=restored["restored_files"],
            restored_bytes=checkpoint["inventory"]["bytes"],
            complete_tree_verified_before_reopen=True,
            old_tree_retained=True,
            independent_reopen_verified=True,
            live_properties_and_netlist_restored=True,
            source_files_unchanged=len(manifest["files"]),
            nonempty_backannotation_applied=False,
            inverse_recovery_verified=False,
            owned_editors_exited=True,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2i-acceptance-designer.json").write_text(
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
    for name in ("server", "python", "sdd", "state", "fixture", "host"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--checkpoint-id", required=True)
    parser.add_argument("--resume-before-restore", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                *(
                    getattr(args, n).resolve()
                    for n in ("server", "python", "sdd", "state", "fixture", "host")
                ),
                args.checkpoint_id,
                args.resume_before_restore
            )
        )
    )
