"""P2h MCP acceptance: nonempty proposals without applying backannotation."""

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from smoke_sessions import Client
from runtime.diagnostics import fingerprint
from runtime.native_packaging import protected_files


def run(server, python, sdd, state, fixture):
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    manifest = json.loads((fixture / "source-manifest.json").read_text())
    protected = protected_files(fixture)
    evidence = state / "evidence" / ("p2h-" + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    (evidence / "protected-before.json").write_text(json.dumps(protected, indent=2))
    report = {
        "status": "running",
        "domain": "designer",
        "project": str(project),
        "evidence": str(evidence),
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

    def invoke(label, tool, arguments, error=None):
        r = client.request("tools/call", {"name": tool, "arguments": arguments})
        value = json.loads(r["content"][0]["text"])
        (evidence / (label + ".json")).write_text(json.dumps(value, indent=2))
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
        )

    def preview(label, mode, baseline=None, error=None):
        args = {"session_id": sid, "design": "CORPORATE", "mode": mode}
        if baseline:
            args["baseline_id"] = baseline["impact_id"]
        return invoke(label, "preview_pads_backannotation", args, error)

    def check_nonempty(value):
        assert value["observed_state_unchanged"] and value["auxiliary_files_written"]
        assert (
            not value["schematic_update_attempted"] and not value["executable_preview"]
        )
        assert (
            not value["impact"]["execution_allowed"] and not value["recovery_verified"]
        )
        counts = value["impact"]["summary"]
        assert counts["reference_changes"] > 0 and counts["pin_changes"] > 0
        assert (
            counts["projected_membership_changes"] > 0
            and counts["fixed_flag_deletions"] > 0
        )
        assert value["impact"]["recovery"]["blockers"]
        assert value["evidence"]["first"]["bam_bytes"] > 0
        if not value["repeatability"]["identical"]:
            assert value["repeatability"]["changed_instances"]
            assert (
                "native_proposal_not_repeatable"
                in value["impact"]["recovery"]["blockers"]
            )

    try:
        report["server_version"] = client.request("initialize")["serverInfo"]["version"]
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        binding = bind("bind")
        sid = binding["session_id"]
        empty = preview("empty_add", "add")
        assert (
            not empty["proposal_records"]
            and empty["impact"]["recovery"]["status"] == "not_needed"
        )
        first = preview("nonempty", "repackage_unfixed")
        check_nonempty(first)
        invoke(
            "execution_refused",
            "execute_pads_packaging",
            {"preview_id": first["impact_id"]},
            "preview_not_ready",
        )
        preview("invalid_mode", "apply", error="invalid_argument")
        preview("scope_mismatch", "add", first, "check_scope_mismatch")
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
        rebound = bind("rebind")
        assert rebound["identity"] == binding["identity"]
        sid = rebound["session_id"]
        after = preview("after_reopen", "repackage_unfixed", first)
        check_nonempty(after)
        assert (
            after["comparison"]["live_equal"] and after["comparison"]["netlist_equal"]
        )
        # Repacking may choose different units/slots; report this instead of
        # pretending a subsequent native execution would replay the first BAM.
        invoke("release", "release_pads_session", {"session_id": sid})
        assert protected_files(fixture) == protected
        assert all(
            fingerprint(Path(manifest["source"]) / p) == h
            for p, h in manifest["files"].items()
        )
        report.update(
            status="passed",
            requests=client.number,
            seconds=round(time.monotonic() - start, 2),
            identity=binding["identity"],
            first_summary=first["impact"]["summary"],
            after_reopen_summary=after["impact"]["summary"],
            first_proposal_repeatable=first["repeatability"]["identical"],
            first_variant_instances=len(first["repeatability"]["changed_instances"]),
            after_reopen_proposal_repeatable=after["repeatability"]["identical"],
            impact_equal_after_reopen=after["comparison"]["impact_equal"],
            live_and_netlist_unchanged_after_reopen=True,
            source_files_unchanged=len(manifest["files"]),
            protected_files_unchanged=len(protected),
            backannotation_impact_preview_verified=True,
            schematic_update_attempted=False,
            auxiliary_files_written=True,
            executable_preview=False,
            recovery_verified=False,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2h-acceptance-designer.json").write_text(
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
