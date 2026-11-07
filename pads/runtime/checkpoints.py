"""Closed-fixture full-tree checkpoints and guarded, non-destructive restoration."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from .diagnostics import fingerprint
from .edits import check_fixture
from .locking import edit_lock
from .probe import existing_editor
from .sessions import PadsError


def inventory(root):
    if root.is_symlink() or root.is_junction() or root.absolute() != root.resolve():
        raise PadsError("checkpoint_link_denied", "Linked tree roots are unsupported")
    root = root.resolve(strict=True)
    files, directories, total = {}, [], 0
    for path in [root, *root.rglob("*")]:
        if (
            path.is_symlink()
            or path.is_junction()
            or not path.resolve().is_relative_to(root)
        ):
            raise PadsError(
                "checkpoint_link_denied", "Linked checkpoint paths are unsupported"
            )
        if path == root:
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            if path.stat().st_nlink != 1:
                raise PadsError(
                    "checkpoint_link_denied", "Hard-linked files are unsupported"
                )
            total += path.stat().st_size
            if len(files) >= 10000 or total > 2 * 1024**3:
                raise PadsError(
                    "checkpoint_limit", "Checkpoint limit is 10000 files and 2 GiB"
                )
            files[relative] = fingerprint(path)
        else:
            if not path.exists():
                raise PadsError(
                    "checkpoint_source_changed",
                    f"File disappeared during inventory: {relative}; wait for native cleanup and create a new preview",
                )
            raise PadsError(
                "checkpoint_file_unsupported",
                f"Only ordinary files/directories are supported: {relative}",
            )
    value = {"files": files, "directories": sorted(directories), "bytes": total}
    value["digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()
    ).hexdigest()
    return value


class Checkpoints:
    def __init__(self, state, editors=existing_editor, clock=time.time):
        if Path(state).absolute() != Path(state).resolve():
            raise PadsError(
                "checkpoint_link_denied", "Linked state directories are unsupported"
            )
        self.state = Path(state).resolve()
        self.editors, self.clock = editors, clock

    def closed(self):
        if any(
            self.editors(domain)
            for domain in ("designer", "layout", "cell_application", "library_manager")
        ):
            raise PadsError(
                "checkpoint_editor_open",
                "Close PADS editors before checkpoint/restore; no editor is closed automatically",
            )

    def scope(self):
        policy_file = self.state / "fixture-edits.json"
        policy = json.loads(policy_file.read_text(encoding="utf-8"))
        if Path(policy["fixture"]).absolute() != Path(policy["fixture"]).resolve():
            raise PadsError(
                "checkpoint_link_denied", "Linked fixture directories are unsupported"
            )
        root = check_fixture(policy, policy["project"])
        if self.state.is_relative_to(root):
            raise PadsError(
                "checkpoint_scope_denied",
                "Checkpoint state must stay outside the fixture",
            )
        return root, fingerprint(policy_file)

    def folder(self, checkpoint_id):
        if not isinstance(checkpoint_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", checkpoint_id
        ):
            raise PadsError("invalid_checkpoint", "Invalid checkpoint ID")
        folder = self.state / "checkpoints" / checkpoint_id
        if folder.resolve() != folder or any(
            p.is_symlink() or p.is_junction() for p in (folder.parent, folder)
        ):
            raise PadsError(
                "checkpoint_link_denied", "Linked checkpoint storage is unsupported"
            )
        return folder

    def load(self, checkpoint_id, root, policy_hash):
        folder = self.folder(checkpoint_id)
        record = json.loads((folder / "manifest.json").read_text())
        if (
            record["checkpoint_id"] != checkpoint_id
            or record["fixture"] != str(root)
            or record["policy_sha256"] != policy_hash
        ):
            raise PadsError(
                "checkpoint_scope_denied",
                "Checkpoint belongs to another fixture/policy",
            )
        if inventory(folder / "tree") != record["inventory"]:
            raise PadsError(
                "checkpoint_corrupt", "Checkpoint contents changed; restoration refused"
            )
        return record, folder

    def create(self):
        with edit_lock(self.state):
            self.closed()
            root, policy_hash = self.scope()
            before = inventory(root)
            checkpoint_id = uuid.uuid4().hex
            folder = self.folder(checkpoint_id)
            folder.mkdir(parents=True, exist_ok=False)
            shutil.copytree(root, folder / "tree")
            if inventory(folder / "tree") != before or inventory(root) != before:
                raise PadsError(
                    "checkpoint_source_changed",
                    "Copy/source changed; incomplete checkpoint retained without a valid manifest",
                )
            self.closed()
            record = {
                "schema": 1,
                "checkpoint_id": checkpoint_id,
                "fixture": str(root),
                "policy_sha256": policy_hash,
                "created_at": self.clock(),
                "inventory": before,
                "editor_closed": True,
                "independent_reopen_verified": False,
            }
            (folder / "manifest.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
            return {
                "checkpoint_id": checkpoint_id,
                "files": len(before["files"]),
                "bytes": before["bytes"],
                "tree_digest": before["digest"],
                "editor_closed": True,
                "independent_reopen_verified": False,
                "project_write_attempted": False,
            }

    def store_restore(self, record, status):
        with sqlite3.connect(self.state / "edits.sqlite") as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS fixture_restores (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO fixture_restores VALUES (?,?,?)",
                (record["preview_id"], status, json.dumps(record)),
            )

    def preview_restore(self, checkpoint_id):
        with edit_lock(self.state):
            self.closed()
            root, policy_hash = self.scope()
            checkpoint, _ = self.load(checkpoint_id, root, policy_hash)
            current, target = inventory(root), checkpoint["inventory"]
            self.closed()
            old, new = current["files"], target["files"]
            record = {
                "preview_id": uuid.uuid4().hex,
                "checkpoint_id": checkpoint_id,
                "fixture": str(root),
                "policy_sha256": policy_hash,
                "current_digest": current["digest"],
                "target_digest": target["digest"],
                "expires_at": self.clock() + 600,
                "file_changes": {
                    "restored": sorted(set(new) - set(old)),
                    "replaced": sorted(
                        p for p in old.keys() & new.keys() if old[p] != new[p]
                    ),
                    "retained_only_in_quarantine": sorted(set(old) - set(new)),
                },
                "current_files": len(old),
                "target_files": len(new),
                "directory_changes": {
                    "restored": sorted(
                        set(target["directories"]) - set(current["directories"])
                    ),
                    "retained_only_in_quarantine": sorted(
                        set(current["directories"]) - set(target["directories"])
                    ),
                },
                "project_write_attempted": False,
                "recovery": "Retain the complete current fixture in a sibling quarantine; copy the checkpoint into a new stage and switch directories only while editors are closed. No deletion.",
            }
            self.store_restore(record, "ready")
            return record

    def execute_restore(self, preview_id):
        if not isinstance(preview_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", preview_id
        ):
            raise PadsError("invalid_preview", "Invalid restore preview ID")
        with edit_lock(self.state):
            self.closed()
            root, policy_hash = self.scope()
            with sqlite3.connect(self.state / "edits.sqlite") as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS fixture_restores (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
                )
                row = db.execute(
                    "SELECT status,record FROM fixture_restores WHERE id=?",
                    (preview_id,),
                ).fetchone()
            if not row or row[0] != "ready":
                raise PadsError(
                    "preview_not_ready",
                    "Restore preview is absent/consumed; do not retry",
                )
            record = json.loads(row[1])
            if self.clock() > record["expires_at"]:
                self.store_restore(record, "expired")
                raise PadsError("preview_expired", "Create a fresh restore preview")
            if (
                record["fixture"] != str(root)
                or record["policy_sha256"] != policy_hash
                or inventory(root)["digest"] != record["current_digest"]
            ):
                raise PadsError(
                    "stale_preview", "Fixture/policy changed after restore preview"
                )
            checkpoint, folder = self.load(record["checkpoint_id"], root, policy_hash)
            if checkpoint["inventory"]["digest"] != record["target_digest"]:
                raise PadsError(
                    "checkpoint_corrupt", "Checkpoint changed after restore preview"
                )
            stage = root.parent / (root.name + "-restore-" + preview_id)
            quarantine = root.parent / (root.name + "-retained-" + preview_id)
            # Verify final absolute rename destinations before any directory move.
            if any(
                p.resolve() != p or p.parent != root.parent or p.exists()
                for p in (stage, quarantine)
            ):
                raise PadsError(
                    "checkpoint_scope_denied",
                    "Restore destinations are not fresh verified sibling paths",
                )
            self.store_restore(record, "preparing")
            moved = False
            try:
                shutil.copytree(folder / "tree", stage)
                if (
                    inventory(stage) != checkpoint["inventory"]
                    or inventory(root)["digest"] != record["current_digest"]
                ):
                    raise PadsError(
                        "stale_preview", "Stage or fixture changed before restoration"
                    )
                self.closed()
                if self.scope() != (root, policy_hash):
                    raise PadsError("stale_preview", "Fixture scope changed")
                self.store_restore(record, "switching")
                root.rename(quarantine)
                moved = True
                stage.rename(root)
                if inventory(root) != checkpoint["inventory"]:
                    raise PadsError(
                        "checkpoint_restore_mismatch",
                        "Restored tree differs; both versions retained for inspection",
                    )
                self.invalidate_records()
                record.update(
                    status="restored",
                    quarantine=str(quarantine),
                    restored_digest=record["target_digest"],
                    restored_files=record["target_files"],
                    project_write_attempted=True,
                    independent_reopen_verified=False,
                )
                self.store_restore(record, "restored")
                return record
            except Exception:
                if moved and not root.exists() and quarantine.exists():
                    quarantine.rename(root)
                self.store_restore(record, "needs_inspection")
                raise

    def invalidate_records(self):
        path = self.state / "sessions.sqlite"
        if path.is_file():
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE sessions SET state='invalidated',reason='fixture_restored' WHERE state='active'"
                )
        with sqlite3.connect(self.state / "edits.sqlite") as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for name in ("edits", "packaging", "sheet_changes", "bam_executions"):
                if name in tables:
                    db.execute(
                        f"UPDATE {name} SET status='invalidated' WHERE status IN ('ready','executed','verified','failed','persistence_verified')"
                    )
            db.execute(
                "UPDATE fixture_restores SET status='invalidated' WHERE status='ready'"
            )
