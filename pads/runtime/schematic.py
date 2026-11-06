"""Durable single-use sheet plans and guarded recovery of recorded changes."""

import json
from pathlib import Path
import uuid
from .edits import Edits, native_edit
from .diagnostics import fingerprint
from .sessions import PadsError
from .schematic_plan import verify_change, normalize


class Schematic(Edits):
    def __init__(self, sdd, state, sessions=None, runner=None, **kwargs):
        super().__init__(sdd, state, sessions, runner, **kwargs)
        if runner is None:
            self.runner = lambda request: native_edit(
                self.sdd, self.state, request, "native_schematic.py"
            )

    def base(self, session_id, design, sheet, action, **extra):
        if any(
            not isinstance(s, str) or not s or len(s) > 256 for s in (design, sheet)
        ):
            raise PadsError(
                "invalid_target", "Existing design and sheet names are required"
            )
        record = self.sessions.read(session_id)
        policy, digest = self.scope(record)
        return {
            "action": action,
            "domain": "designer",
            "expected_path": record["identity"]["project_path"],
            "expected_identity": record["identity"],
            "design": design,
            "sheet": sheet,
            "policy": policy,
            "policy_sha256": digest,
            **extra,
        }

    def inspect_sheet(self, session_id, design, sheet):
        with self.lock():
            return self.checked(
                self.runner(self.base(session_id, design, sheet, "inspect"))
            )

    def inspect_logical(self, session_id, design, pins, baseline_id=None):
        from .logical import validate_pins, compare_reports

        validate_pins(pins)
        with self.lock():
            request = self.base(session_id, design, "*", "logical", pins=pins)
            previous = None
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS logical_checks (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
                )
                if baseline_id is not None:
                    row = db.execute(
                        "SELECT record FROM logical_checks WHERE id=?", (baseline_id,)
                    ).fetchone()
                    if row is None:
                        raise PadsError(
                            "check_not_found", "Logical baseline was not found"
                        )
                    previous = json.loads(row[0])
                    self.same_scope(previous, request)
                    if previous["design"] != design or sorted(
                        (p["refdes"], p["pin_number"]) for p in previous["pins"]
                    ) != sorted((p["refdes"], p["pin_number"]) for p in pins):
                        raise PadsError(
                            "check_scope_mismatch",
                            "Logical baseline uses another design or selected pin set",
                        )
            check_id = uuid.uuid4().hex
            request["output_dir"] = str(
                (self.state / "evidence" / ("logical-" + check_id)).resolve()
            )
            result = self.checked(self.runner(request))
            self.sessions.load(session_id)
            record = {
                "check_id": check_id,
                "design": design,
                "pins": pins,
                "project": request["expected_path"],
                "policy_sha256": request["policy_sha256"],
                **result,
            }
            if previous:
                record["comparison"] = compare_reports(previous, record)
                record["baseline_id"] = baseline_id
            with self.db() as db:
                db.execute(
                    "INSERT INTO logical_checks VALUES (?,?)",
                    (check_id, json.dumps(record)),
                )
            return record

    def inspect_pin_mapping(self, session_id, design, refdes, baseline_id=None):
        from .pin_mapping import validate_refs

        validate_refs(refdes)
        with self.lock():
            request = self.base(session_id, design, "*", "pin_mapping", refdes=refdes)
            previous = None
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS mapping_checks (id TEXT PRIMARY KEY,record TEXT NOT NULL)"
                )
                if baseline_id is not None:
                    row = db.execute(
                        "SELECT record FROM mapping_checks WHERE id=?", (baseline_id,)
                    ).fetchone()
                    if row is None:
                        raise PadsError(
                            "check_not_found", "Mapping baseline was not found"
                        )
                    previous = json.loads(row[0])
                    self.same_scope(previous, request)
                    if previous["design"] != design or sorted(
                        previous["refdes"]
                    ) != sorted(refdes):
                        raise PadsError(
                            "check_scope_mismatch",
                            "Mapping baseline design/reference scope differs",
                        )
            check_id = uuid.uuid4().hex
            request["output_dir"] = str(
                (self.state / "evidence" / ("mapping-" + check_id)).resolve()
            )
            result = self.checked(self.runner(request))
            self.sessions.load(session_id)
            record = {
                "check_id": check_id,
                "design": design,
                "refdes": refdes,
                "project": request["expected_path"],
                "policy_sha256": request["policy_sha256"],
                **result,
            }
            if previous:
                if previous["provenance"] != record["provenance"]:
                    raise PadsError(
                        "mapping_baseline_changed",
                        "Effective PDB, native tools or mapping contract changed; create a fresh baseline",
                    )
                record.update(
                    baseline_id=baseline_id,
                    comparison={
                        "mapping_equal": previous["mapping"] == record["mapping"],
                        "parts_equal": previous["parts"] == record["parts"],
                    },
                )
            with self.db() as db:
                db.execute(
                    "INSERT INTO mapping_checks VALUES (?,?)",
                    (check_id, json.dumps(record)),
                )
            return record

    def check_sheet(
        self,
        session_id,
        design,
        sheet,
        baseline_id=None,
        profile="connectivity",
        scope="sheet",
    ):
        from .electrical import compare_checks, check_options

        check_options(profile, scope)
        with self.lock():
            request = self.base(
                session_id, design, sheet, "check", profile=profile, check_scope=scope
            )
            previous = None
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS checks (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
                )
                if baseline_id is not None:
                    row = db.execute(
                        "SELECT record FROM checks WHERE id=?", (baseline_id,)
                    ).fetchone()
                    if row is None:
                        raise PadsError(
                            "check_not_found", "Recorded baseline check was not found"
                        )
                    previous = json.loads(row[0])
                    if (
                        previous.get("profile", "connectivity") != profile
                        or previous.get("check_scope", "sheet") != scope
                    ):
                        raise PadsError(
                            "check_scope_mismatch",
                            "Baseline uses another rule profile or check scope",
                        )
                    self.same_scope(previous, request)
                    if (previous["design"], previous["sheet"]) != (design, sheet):
                        raise PadsError(
                            "check_scope_mismatch", "Baseline belongs to another sheet"
                        )
            check_id = uuid.uuid4().hex
            request["output_dir"] = str(
                (self.state / "evidence" / ("check-" + check_id)).resolve()
            )
            result = self.checked(self.runner(request))
            record = {
                "check_id": check_id,
                "project": request["expected_path"],
                "policy_sha256": request["policy_sha256"],
                "design": design,
                "sheet": sheet,
                "profile": profile,
                "check_scope": scope,
                **result,
            }
            if previous:
                record["comparison"] = compare_checks(previous, record)
                record["baseline_id"] = baseline_id
            with self.db() as db:
                db.execute(
                    "INSERT INTO checks VALUES (?,?)", (check_id, json.dumps(record))
                )
            return record

    def insert(self, record):
        with self.db() as db:
            db.execute(
                "INSERT INTO edits VALUES (?,?,?)",
                (record["preview_id"], "ready", json.dumps(record)),
            )
        return record

    def preview_change(self, session_id, design, sheet, operation):
        with self.lock():
            request = self.base(
                session_id, design, sheet, "preview", operation=operation
            )
            result = self.checked(self.runner(request))
            self.sessions.load(session_id)
            return self.insert(
                {
                    "schema": 2,
                    "preview_id": uuid.uuid4().hex,
                    "session_id": session_id,
                    "design": design,
                    "sheet": sheet,
                    "plan": result["plan"],
                    "action": "execute",
                    "expires_at": self.clock() + 600,
                    "expected_sheet": result["plan"]["before"],
                    "library_sha256": result["library_sha256"],
                    "project": request["expected_path"],
                    "policy_sha256": request["policy_sha256"],
                    "project_write_attempted": False,
                }
            )

    def previous(self, change_id):
        if (
            not isinstance(change_id, str)
            or len(change_id) != 32
            or any(c not in "0123456789abcdef" for c in change_id)
        ):
            raise PadsError("invalid_preview", "Invalid change ID")
        with self.db() as db:
            row = db.execute(
                "SELECT status,record FROM edits WHERE id=?", (change_id,)
            ).fetchone()
        if row is None:
            raise PadsError("preview_not_ready", "Recorded change was not found")
        record = json.loads(row[1])
        if record.get("schema") != 2:
            raise PadsError(
                "invalid_preview", "This tool requires a sheet-level change record"
            )
        record["plan"]["before"] = normalize(record["plan"]["before"])
        if "expected" in record["plan"]:
            record["plan"]["expected"] = normalize(record["plan"]["expected"])
        record["expected_sheet"] = normalize(record["expected_sheet"])
        if "sheet" in record.get("result", {}):
            record["result"]["sheet"] = normalize(record["result"]["sheet"])
        return row[0], record

    def preview_recovery(self, session_id, change_id):
        from .native_schematic import recovery_allowed

        with self.lock():
            status, previous = self.previous(change_id)
            if (
                previous["action"] != "execute"
                or status not in ("executed", "verified", "failed")
                or "sheet" not in previous.get("result", {})
            ):
                raise PadsError(
                    "recovery_unavailable",
                    "No observed, reversible result is recorded; inspect the test copy",
                )
            request = self.base(
                session_id, previous["design"], previous["sheet"], "inspect"
            )
            self.same_scope(previous, request)
            result = self.checked(self.runner(request))
            if (
                result["sheet"] != previous["result"]["sheet"]
                or result["library_sha256"] != previous["library_sha256"]
            ):
                raise PadsError(
                    "stale_recovery",
                    "Sheet or library changed after this operation; later edits will not be overwritten",
                )
            recovery_allowed(previous["plan"], result["sheet"])
            return self.insert(
                {
                    "schema": 2,
                    "preview_id": uuid.uuid4().hex,
                    "session_id": session_id,
                    "design": previous["design"],
                    "sheet": previous["sheet"],
                    "plan": previous["plan"],
                    "action": "recover",
                    "original_change_id": change_id,
                    "expires_at": self.clock() + 600,
                    "expected_sheet": result["sheet"],
                    "recovery_target": previous["plan"]["before"],
                    "library_sha256": previous["library_sha256"],
                    "project": previous["project"],
                    "policy_sha256": previous["policy_sha256"],
                    "project_write_attempted": False,
                }
            )

    @staticmethod
    def same_scope(record, request):
        if (
            record["project"] != request["expected_path"]
            or record["policy_sha256"] != request["policy_sha256"]
        ):
            raise PadsError(
                "write_scope_changed",
                "Change belongs to another configured test project",
            )

    def execute_change(self, preview_id):
        with self.lock():
            status, record = self.previous(preview_id)
            if status != "ready":
                raise PadsError(
                    "preview_not_ready", "Preview has already been consumed"
                )
            if self.clock() > record["expires_at"]:
                self.persist(record, "expired")
                raise PadsError("preview_expired", "Create a fresh preview")
            request = self.base(
                record["session_id"],
                record["design"],
                record["sheet"],
                record["action"],
                expected_sheet=record["expected_sheet"],
                plan=record["plan"],
                library_sha256=record["library_sha256"],
                expires_at=record["expires_at"],
            )
            self.same_scope(record, request)
            self.persist(record, "executing")
            try:
                result = self.runner(request)
            except Exception:
                self.persist(record, "outcome_unknown")
                self.sessions.invalidate(
                    record["session_id"], "schematic_outcome_unknown"
                )
                raise
            if result.get("ok") is True:
                try:
                    if record["action"] == "execute":
                        verify_change(record["plan"], result["sheet"])
                    elif result["sheet"] != record["plan"]["before"]:
                        raise PadsError(
                            "readback_mismatch", "Recovery differs from baseline"
                        )
                except (PadsError, KeyError) as exc:
                    result = {
                        **result,
                        "ok": False,
                        "error": {"kind": "readback_mismatch", "message": str(exc)},
                        "project_write_attempted": True,
                    }
            record["result"] = result
            self.persist(record, "executed" if result.get("ok") is True else "failed")
            if result.get("ok") is not True:
                if result.get("project_write_attempted") is not False:
                    self.sessions.invalidate(
                        record["session_id"], "schematic_needs_inspection"
                    )
                return {"preview_id": preview_id, **result}
            if record["action"] == "recover":
                _, original = self.previous(record["original_change_id"])
                self.persist(original, "recovered")
            current = self.sessions.load(record["session_id"])
            current.update(
                mode="test_fixture_edited",
                project_write_attempted=True,
                project_sha256=fingerprint(Path(record["project"])),
            )
            with self.sessions.db() as db:
                db.execute(
                    "UPDATE sessions SET record=? WHERE id=? AND state='active'",
                    (json.dumps(current), record["session_id"]),
                )
            return {"preview_id": preview_id, "status": "executed", **result}

    def reopen_change(self, session_id, change_id):
        with self.lock():
            status, record = self.previous(change_id)
            if status != "executed":
                raise PadsError(
                    "preview_not_ready",
                    "Only a successful unverified change can be reopened",
                )
            request = self.base(
                session_id,
                record["design"],
                record["sheet"],
                "reopen",
                expected_sheet=record["result"]["sheet"],
                library_sha256=record["library_sha256"],
            )
            self.same_scope(record, request)
            self.persist(record, "reopening")
            try:
                result = self.runner(request)
            except Exception:
                self.persist(record, "outcome_unknown")
                raise
            finally:
                self.sessions.invalidate(session_id, "schematic_reopen_bind_again")
            if (
                result.get("ok") is True
                and result.get("sheet") != record["result"]["sheet"]
            ):
                result = {
                    **result,
                    "ok": False,
                    "error": {
                        "kind": "readback_mismatch",
                        "message": "Reopened sheet differs from recorded result",
                    },
                }
            record["reopen_result"] = result
            self.persist(record, "verified" if result.get("ok") is True else "failed")
            return {
                "preview_id": change_id,
                "status": (
                    "persistence_verified" if result.get("ok") is True else "failed"
                ),
                **result,
            }
