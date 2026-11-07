import copy
import json
from pathlib import Path

import pytest

from test_edits import context, expect, TARGET, CHANGE
from runtime.attribute_state import parse_batch, property_state, scope_guard, FROZEN
from runtime.checkpoints import Checkpoints, inventory
from runtime.sessions import PadsError


def test_batch_presence_distinguishes_absent_empty_and_nonempty_overrides():
    base = parse_batch("0 Tolerance=5%\r0 Frozen Package=\r")
    absent = property_state(base, {}, "Tolerance")
    empty = property_state(base, parse_batch("0 Tolerance=\r"), "Tolerance")
    value = property_state(base, parse_batch("3 Tolerance=1%\r"), "Tolerance")
    assert not absent["instance_present"] and absent["effective"] == "5%"
    assert empty["instance_present"] and empty["instance"] == empty["effective"] == ""
    assert value["instance_present"] and value["effective"] == "1%"
    assert property_state(base, {}, FROZEN)["base_present"]
    assert parse_batch("0 other=a=b\r")["other"]["value"] == "a=b"


@pytest.mark.parametrize(
    "text", ["oops", "0 x=a\r0 x=b\r", "0 x=a\nbroken", "0 x=\x00", None]
)
def test_bad_batch_data_fails_closed(text):
    with pytest.raises(PadsError):
        parse_batch(text)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "outside_unfrozen",
        "empty_flag",
        "one_flag",
        "missing_preimage",
        "split_package",
        "missing_sheet",
        "many_selected",
        "bad_sheet",
    ],
)
def test_native_scope_requires_frozen_outside_and_no_split_units(fault):
    unfrozen = property_state({}, {}, FROZEN)
    frozen = property_state(parse_batch("0 Frozen Package=Fix\r"), {}, FROZEN)
    live = [
        {"scope": "d(power)", "refdes": "C1", "frozen": unfrozen},
        {"scope": "d(other)", "refdes": "R1", "frozen": frozen},
    ]
    sheet = "power"
    if fault == "outside_unfrozen":
        live[1]["frozen"] = unfrozen
    if fault == "empty_flag":
        live[1]["frozen"] = property_state(
            parse_batch("0 Frozen Package=\r"), {}, FROZEN
        )
    if fault == "one_flag":
        live[1]["frozen"] = property_state(
            {}, parse_batch("0 Frozen Package=1\r"), FROZEN
        )
    if fault == "missing_preimage":
        live[0]["frozen"] = {}
    if fault == "split_package":
        live[1]["refdes"] = "C1"
    if fault == "missing_sheet":
        sheet = "absent"
    if fault == "many_selected":
        live += [copy.deepcopy(live[0])] * 16
    if fault == "bad_sheet":
        sheet = "power]-r"
    if fault:
        with pytest.raises(PadsError):
            scope_guard(live, "d", sheet)
    else:
        assert scope_guard(live, "d", sheet) == "d(power)"


def fixture_service(context):
    service, sid, _, _, root = context
    checkpoint = Checkpoints(
        service.state, editors=lambda domain: [], clock=lambda: 1000
    )
    (root / "empty").mkdir()
    (root / "Library").mkdir()
    (root / "Library/data.bin").write_bytes(b"library original")
    return checkpoint, root, service, sid


def test_complete_restore_preserves_old_tree_and_invalidates_old_bindings(context):
    checkpoints, root, edits, sid = fixture_service(context)
    before = inventory(root)
    snapshot = checkpoints.create()
    old_preview = edits.preview(sid, TARGET, CHANGE)
    (root / "Library/data.bin").write_bytes(b"changed")
    (root / "new.txt").write_text("retained")
    (root / "empty").rmdir()
    current = inventory(root)
    preview = checkpoints.preview_restore(snapshot["checkpoint_id"])
    assert "Library/data.bin" in preview["file_changes"]["replaced"]
    assert "new.txt" in preview["file_changes"]["retained_only_in_quarantine"]
    assert preview["directory_changes"]["restored"] == ["empty"]
    result = checkpoints.execute_restore(preview["preview_id"])
    assert inventory(root) == before
    assert inventory(Path(result["quarantine"])) == current
    assert not result["independent_reopen_verified"]
    expect("session_invalidated", lambda: edits.sessions.load(sid))
    expect("preview_not_ready", lambda: edits.run(old_preview["preview_id"]))
    expect(
        "preview_not_ready", lambda: checkpoints.execute_restore(preview["preview_id"])
    )


@pytest.mark.parametrize("phase", ["create", "preview", "execute"])
def test_open_editors_always_block_checkpoint_or_restore(context, phase):
    checkpoints, root, *_ = fixture_service(context)
    snapshot = checkpoints.create()
    preview = checkpoints.preview_restore(snapshot["checkpoint_id"])
    before = inventory(root)
    checkpoints.editors = lambda domain: [42] if domain == "layout" else []
    action = {
        "create": checkpoints.create,
        "preview": lambda: checkpoints.preview_restore(snapshot["checkpoint_id"]),
        "execute": lambda: checkpoints.execute_restore(preview["preview_id"]),
    }[phase]
    expect("checkpoint_editor_open", action)
    assert inventory(root) == before


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "corrupt",
        "expired",
        "policy",
        "wrong_target",
        "invalid_id",
        "checkpoint_replaced",
    ],
)
def test_restore_refuses_changed_inputs_before_any_move(context, fault):
    checkpoints, root, *_ = fixture_service(context)
    snapshot = checkpoints.create()
    preview = checkpoints.preview_restore(snapshot["checkpoint_id"])
    if fault == "stale":
        (root / "new.txt").write_text("later edit")
    if fault == "corrupt":
        (
            checkpoints.folder(snapshot["checkpoint_id"]) / "tree/Library/data.bin"
        ).write_bytes(b"corrupt")
    if fault == "expired":
        checkpoints.clock = lambda: 2000
    if fault == "policy":
        path = checkpoints.state / "fixture-edits.json"
        path.write_text(path.read_text() + " ")
    if fault in ("wrong_target", "checkpoint_replaced"):
        path = checkpoints.folder(snapshot["checkpoint_id"]) / "manifest.json"
        data = json.loads(path.read_text())
        if fault == "wrong_target":
            data["fixture"] = str(root.parent)
        else:
            tree = checkpoints.folder(snapshot["checkpoint_id"]) / "tree"
            (tree / "Library/data.bin").write_bytes(b"changed together")
            data["inventory"] = inventory(tree)
        path.write_text(json.dumps(data))
    before = inventory(root)
    with pytest.raises(PadsError):
        checkpoints.execute_restore(
            "../bad" if fault == "invalid_id" else preview["preview_id"]
        )
    assert inventory(root) == before
    assert not list(root.parent.glob(root.name + "-retained-*"))


@pytest.mark.parametrize("failure_at", ["copy", "first_move", "second_move"])
def test_restore_io_failure_keeps_original_and_consumes_preview(
    context, monkeypatch, failure_at
):
    import runtime.checkpoints as module

    checkpoints, root, *_ = fixture_service(context)
    snapshot = checkpoints.create()
    (root / "Library/data.bin").write_bytes(b"keep this edited version")
    before = inventory(root)
    preview = checkpoints.preview_restore(snapshot["checkpoint_id"])
    original_rename = Path.rename

    def rename(path, target):
        if (failure_at == "first_move" and path == root) or (
            failure_at == "second_move" and "-restore-" in path.name
        ):
            raise OSError("injected sharing violation")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    if failure_at == "copy":
        monkeypatch.setattr(
            module.shutil,
            "copytree",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected copy failure")
            ),
        )
    with pytest.raises(OSError):
        checkpoints.execute_restore(preview["preview_id"])
    assert inventory(root) == before
    expect(
        "preview_not_ready", lambda: checkpoints.execute_restore(preview["preview_id"])
    )


def test_linked_checkpoint_file_is_refused(context):
    import os

    checkpoints, root, *_ = fixture_service(context)
    os.link(root / "Library/data.bin", root / "linked.bin")
    with pytest.raises(PadsError):
        checkpoints.create()


def test_disappearing_file_is_not_silently_omitted(context, monkeypatch):
    _, root, *_ = fixture_service(context)
    real_rglob = Path.rglob

    def listing(path, *args, **kwargs):
        found = list(real_rglob(path, *args, **kwargs))
        return iter(
            found + [root / "removed-by-native-cleanup.tmp"] if path == root else found
        )

    monkeypatch.setattr(Path, "rglob", listing)
    expect("checkpoint_source_changed", lambda: inventory(root))


def test_acceptance_waits_for_stable_inventory_without_retrying_restore(
    monkeypatch, tmp_path
):
    import smoke_checkpoint as smoke

    values = iter(
        [
            PadsError("checkpoint_source_changed", "cleanup"),
            {"digest": "one"},
            {"digest": "two"},
            {"digest": "two"},
        ]
    )

    def read(_):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(smoke, "inventory", read)
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: None)
    assert smoke.settled_inventory(tmp_path) == {"digest": "two"}


@pytest.mark.parametrize(
    "status", ["executed", "verified", "failed", "persistence_verified"]
)
def test_restore_invalidates_old_mutation_and_partial_recovery_records(context, status):
    checkpoints, root, edits, sid = fixture_service(context)
    snapshot = checkpoints.create()
    old = edits.preview(sid, TARGET, CHANGE)
    edits.persist(old, status)
    preview = checkpoints.preview_restore(snapshot["checkpoint_id"])
    checkpoints.execute_restore(preview["preview_id"])
    with edits.db() as db:
        assert (
            db.execute(
                "SELECT status FROM edits WHERE id=?", (old["preview_id"],)
            ).fetchone()[0]
            == "invalidated"
        )
