"""P2g real MCP read-only mapping acceptance on a prepared evaluation copy."""

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from smoke_sessions import Client
from runtime.diagnostics import fingerprint
from runtime.native_packaging import protected_files


def reopen(binding_file, fixture):
    """Private acceptance lifecycle helper; never exposed as an MCP edit tool."""
    from runtime.native_designer import connect, call
    from runtime.native_schematic import existing_sheet

    binding = json.loads(binding_file.read_text())
    expected = binding["identity"]
    project = Path(expected["project_path"]).resolve(strict=True)
    fixture = fixture.resolve(strict=True)
    assert (fixture / "source-manifest.json").is_file()
    assert project == fixture / "LessonFinal/LessonFinal.prj"
    app, identity = connect(
        {"expected_path": str(project), "expected_identity": expected}
    )
    assert identity() == expected
    assert call(app, "CloseProject") and not call(app, "IsProjectOpened")
    call(app, "OpenProject", str(project))
    assert (
        Path(call(call(app, "GetProjectData"), "GetProjectFilePath")).resolve()
        == project
    )
    existing_sheet(app, "CORPORATE", "Powersupply")
    assert identity() == expected


def run(server, python, sdd, state, fixture):
    project = (fixture / "LessonFinal/LessonFinal.prj").resolve(strict=True)
    manifest = json.loads((fixture / "source-manifest.json").read_text())
    protected = protected_files(fixture)
    evidence = state / "evidence" / ("p2g-" + uuid.uuid4().hex[:8])
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
        value = client.call(tool, arguments, error)
        (evidence / (label + ".json")).write_text(json.dumps(value, indent=2))
        report["steps"].append(
            {"step": label, "seconds": round(time.monotonic() - start, 2)}
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        print(label, flush=True)
        return value

    def bind(label):
        return invoke(
            label,
            "bind_pads_session",
            {"domain": "designer", "expected_project": str(project)},
        )

    def mapping(label, baseline=None, refs=None, error=None):
        args = {
            "session_id": sid,
            "design": "CORPORATE",
            "refdes": refs or ["U12", "U6", "U4"],
        }
        if baseline:
            args["baseline_id"] = baseline["check_id"]
        return invoke(label, "inspect_pads_pin_mapping", args, error)

    try:
        report["server_version"] = client.request("initialize")["serverInfo"]["version"]
        client.call("load_toolset", {"name": "session"})
        client.call("load_toolset", {"name": "edit"})
        binding = bind("bind")
        sid = binding["session_id"]
        mapping(
            "missing_reference", refs=["U99999"], error="mapping_reference_not_found"
        )
        first = mapping("baseline")
        assert first["all_selected_live_instances_corroborated"]
        assert not first["write_authorized"] and not first["project_write_attempted"]
        assert not first["source"]["central_library_equivalence_verified"]
        rows = {r["refdes"]: r for r in first["mapping"]}
        pins = rows["U12"]["instances"][0]["pins"]
        assert {p["name"]: p["physical_number"] for p in pins} == {
            "VIN": "4",
            "GND": "5",
            "VOUT": "1",
            "BYPASS": "3",
            "ON/~OFF": "2",
        }
        assert all(
            p["number"] == "" and p["number_source"] == "unique_library_slot"
            for p in pins
        )
        assert {i["slot_id"] for i in rows["U6"]["instances"]} == {1, 2}
        shared = rows["U6"]["shared_physical_pins"]
        assert {p["physical_number"] for p in shared} == {"4", "8"}
        assert all(
            len(p["live_occurrences"]) == 2 and not p["single_live_endpoint"]
            for p in shared
        )
        hidden = rows["U4"]["non_symbol_supply_pins"]
        assert {p["physical_number"]: p["library_supply_name"] for p in hidden} == {
            "1": "VDD",
            "4": "GND",
        }
        assert all(
            p["corroborated"] and not p["live_endpoint_available"] for p in hidden
        )
        second = mapping("repeat", first)
        assert all(second["comparison"].values())
        mapping("scope_mismatch", first, ["U12"], "check_scope_mismatch")
        for ref, number in (("U12", "4"), ("U6", "8")):
            invoke(
                "no_new_endpoint_" + ref,
                "inspect_pads_logical_connectivity",
                {
                    "session_id": sid,
                    "design": "CORPORATE",
                    "pins": [{"refdes": ref, "pin_number": number}],
                },
                "logical_pin_not_unique",
            )
        invoke("release_before_reopen", "release_pads_session", {"session_id": sid})
        subprocess.run(
            [
                str(python),
                str(Path(__file__).resolve()),
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
        third = mapping("after_reopen", first)
        assert (
            all(third["comparison"].values())
            and third["all_selected_live_instances_corroborated"]
        )
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
            selected_library_pin_mapping_verified=True,
            implicit_number_reference="U12",
            shared_power_reference="U6",
            non_symbol_supply_reference="U4",
            mapping_equal_after_reopen=True,
            source_files_unchanged=len(manifest["files"]),
            copied_protected_files_unchanged=len(protected),
            project_write_attempted=False,
            write_authorized=False,
            full_design_pin_mapping_verified=False,
        )
        (evidence / "report.json").write_text(json.dumps(report, indent=2))
        (state / "evidence/p2g-acceptance-designer.json").write_text(
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
    parser.add_argument("--reopen-binding", type=Path)
    for name in ("server", "python", "sdd", "state", "fixture"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.reopen_binding:
        reopen(args.reopen_binding, args.fixture)
    else:
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
