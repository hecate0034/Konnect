import copy
import pytest
from test_geometry import sheet, comp, target
from runtime.routing import segment, netlist
from runtime.schematic_plan import plan, verify_change, normalize
from runtime.native_schematic import recovery_allowed
from runtime.sessions import PadsError
from runtime.electrical import parse_verify_output, compare_checks, verify_profile


def route_op(sheet):
    return {
        "kind": "route",
        "a": {"target": target(sheet, "a"), "pin_uid": "p"},
        "b": {"target": target(sheet, "b"), "pin_uid": "p"},
        "unit": "0.01_inch",
        "points": [[10, 5], [20, 5], [20, 20], [30, 20], [30, 5], [40, 5]],
    }


def connected(sheet):
    after = copy.deepcopy(sheet)
    after["nets"] = [
        {
            "uid": "n",
            "name": "n",
            "segments": [
                segment([10, 5], [20, 5], 2, 3),
                segment([20, 5], [20, 20], 3, 3),
                segment([20, 20], [30, 20], 3, 3),
                segment([30, 20], [30, 5], 3, 3),
                segment([30, 5], [40, 5], 3, 2),
            ],
        }
    ]
    for c in after["components"]:
        c["pins"][0]["net_uid"] = "n"
        c["connections"][0].update(net_uid="n", net_name="n")
    return normalize(after)


def test_multisegment_exact_delta_and_partial_prefix_recovery(sheet):
    p = plan(sheet, route_op(sheet))
    after = connected(sheet)
    verify_change(p, after)
    recovery_allowed(p, after)
    partial = copy.deepcopy(sheet)
    partial["nets"] = [
        {"uid": "n", "name": "n", "segments": [segment([10, 5], [20, 5], 2, 1)]}
    ]
    c = next(c for c in partial["components"] if c["target"]["uid"] == "a")
    c["pins"][0]["net_uid"] = "n"
    c["connections"][0].update(net_uid="n", net_name="n")
    recovery_allowed(p, partial)
    partial["nets"][0]["segments"][0]["b"] = [21, 5]
    with pytest.raises(PadsError):
        recovery_allowed(p, partial)


@pytest.mark.parametrize(
    "points",
    [
        [[10, 5], [40, 6]],
        [[10, 5], [10, 5], [40, 5]],
        [[10, 5], [20, 5], [40, 5]],
        [[10, 5], [False, 20], [40, 5]],
        [[10, 5], [20, 5], [20, 20], [15, 20], [15, 0], [40, 0], [40, 5]],
        [[11, 5], [20, 5], [20, 20], [40, 20], [40, 5]],
        [[10, 5], [-1, 5], [-1, 20], [40, 20], [40, 5]],
    ],
)
def test_invalid_route_geometry_is_refused(sheet, points):
    op = route_op(sheet)
    op["points"] = points
    with pytest.raises(PadsError):
        plan(sheet, op)


def test_branch_preserves_old_net_identity_and_can_recover_observed_prefix(sheet):
    before = connected(sheet)
    c = comp("c", 70, "R3")
    c["bbox"] = [[70, 15], [90, 25]]
    c["position"]["y"] = 20
    c["pins"][0]["point"] = [80, 25]
    before["components"].append(c)
    before = normalize(before)
    op = {
        "kind": "route",
        "a": {"target": c["target"], "pin_uid": "p"},
        "b": {"net_uid": "n", "net_name": "n", "point": [20, 20]},
        "unit": "0.01_inch",
        "points": [[80, 25], [80, 40], [20, 40], [20, 20]],
    }
    p = plan(before, op)
    after = copy.deepcopy(before)
    for s in after["nets"][0]["segments"]:
        for k in ("a", "b"):
            if s[k] == [20, 20]:
                s["joint_" + k] = 5
    # Native execution starts at the existing net; a known one-segment prefix is reversible.
    after["nets"][0]["segments"].append(segment([20, 20], [20, 40], 5, 1))
    recovery_allowed(p, normalize(after))
    after["nets"][0]["segments"][-1] = segment([20, 20], [20, 40], 5, 3)
    after["nets"][0]["segments"] += [
        segment([20, 40], [80, 40], 3, 3),
        segment([80, 40], [80, 25], 3, 2),
    ]
    c = next(c for c in after["components"] if c["target"]["uid"] == "c")
    c["pins"][0]["net_uid"] = "n"
    c["connections"][0].update(net_uid="n", net_name="n")
    verify_change(p, normalize(after))
    assert len(netlist(after)["nets"][0]["pins"]) == 3
    after["nets"][0]["uid"] = "replaced"
    with pytest.raises(PadsError):
        verify_change(p, after)
    op["b"]["point"] = [25, 20]
    op["points"][-1] = [25, 20]
    with pytest.raises(PadsError) as e:
        plan(before, op)
    assert e.value.kind == "anchor_unsupported"


@pytest.mark.parametrize("corner", [False, True])
def test_tangent_pin_exit_is_refused_instead_of_accepting_native_detours(sheet, corner):
    op = route_op(sheet)
    op["points"] = [[10, 5], [20, 5], [20, 20], [40, 20], [40, 5]]
    if corner:
        next(c for c in sheet["components"] if c["target"]["uid"] == "a")["pins"][0][
            "point"
        ] = [10, 10]
        op["points"] = [[10, 10], [20, 10], [20, 20], [30, 20], [30, 5], [40, 5]]
    with pytest.raises(PadsError) as e:
        plan(sheet, op)
    assert e.value.kind == "pin_exit_unsupported"


@pytest.mark.parametrize(
    "stdout,stderr,code",
    [
        ("", "", 0),
        ("Error : Wrong parameter list!", "", 0),
        ("0 Error(s), 0 Warning(s), 0 Note(s),", "failure", 0),
        ("0 Error(s), 0 Warning(s), 0 Note(s),", "", 1),
        ("0 Error(s), 0 Warning(s), 0 Note(s),\npartial output", "", 0),
        ("1 Error(s), 0 Warning(s), 0 Note(s),", "", 0),
    ],
)
def test_native_checker_never_treats_exit_zero_as_sufficient(stdout, stderr, code):
    with pytest.raises(PadsError):
        parse_verify_output(stdout, stderr, code)


def test_native_checker_parses_findings_without_claiming_full_erc():
    r = parse_verify_output(
        "Error drc-107: pin not connected\n1 Error(s), 0 Warning(s), 0 Note(s),", "", 0
    )
    assert (
        r["counts"]["errors"] == 1
        and not r["profile_clean"]
        and not r["full_erc_verified"]
    )
    assert parse_verify_output("0 Error(s), 0 Warning(s), 0 Note(s),", "", 0)[
        "profile_clean"
    ]


def test_check_comparison_refuses_changed_rule_provenance(sheet):
    before = {
        "native_verify": {
            "profile_sha256": "a",
            "input_sha256": {},
            "counts": {"errors": 0, "warnings": 0, "notes": 0},
            "findings_text": "",
        },
        "connectivity": netlist(sheet),
    }
    after = copy.deepcopy(before)
    after["connectivity"] = netlist(connected(sheet))
    delta = compare_checks(before, after)
    assert not delta["netlist_equal"] and len(delta["nets_after_only"]) == 1
    after["native_verify"]["profile_sha256"] = "b"
    with pytest.raises(PadsError):
        compare_checks(before, after)


def test_native_profile_enables_only_the_three_named_checks():
    import xml.etree.ElementTree as ET

    original = (
        b'<VDRC><Checks><Check Name="Other" State="Enabled"/>'
        + b"".join(
            b'<Check Name="' + n + b'" State="Disabled"/>'
            for n in (b"UnconnectedPins", b"ZeroPin", b"NetOverlap")
        )
        + b"</Checks></VDRC>"
    )
    profile = verify_profile(original)
    assert b'<Check Name="Other" State="Disabled"/>' in profile
    assert (
        sum(c.get("State") == "Enabled" for c in ET.fromstring(profile).iter("Check"))
        == 3
    )
    with pytest.raises(PadsError):
        verify_profile(original.replace(b"ZeroPin", b"Missing"))


def test_native_check_refuses_rule_changes_during_execution(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from runtime import electrical

    sdd = tmp_path / "sdd"
    exe = sdd / "wv/win32/bin/vdrc.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake-checker")
    defaults = sdd / "standard/VerifyDefaults.ini"
    defaults.parent.mkdir()
    defaults.write_bytes(
        b"<VDRC><Checks>"
        + b"".join(
            b'<Check Name="' + n + b'" State="Disabled"/>'
            for n in (b"UnconnectedPins", b"ZeroPin", b"NetOverlap")
        )
        + b"</Checks></VDRC>"
    )
    monkeypatch.setenv("SDD_HOME", str(sdd))

    def run(command, **kwargs):
        config = Path(command[command.index("-def") + 1])
        config.write_bytes(config.read_bytes() + b" ")
        return SimpleNamespace(
            returncode=0, stdout=b"0 Error(s), 0 Warning(s), 0 Note(s),", stderr=b""
        )

    monkeypatch.setattr(electrical.subprocess, "run", run)
    with pytest.raises(PadsError) as e:
        electrical.run_verify(tmp_path / "test.prj", "d", "s", tmp_path / "evidence")
    assert e.value.kind == "native_check_inputs_changed"
