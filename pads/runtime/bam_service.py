"""Single-use approved-file plans, separate from historical impact reports."""

import hashlib
import json
from pathlib import Path
import re
import uuid

from .bam_execution import read_approved, require
from .checkpoints import Checkpoints
from .schematic import Schematic
from .sessions import PadsError


class BamExecution(Schematic):
    def table(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS bam_executions (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
        )

    def save(self, record, status):
        with self.db() as db:
            self.table(db)
            db.execute(
                "INSERT OR REPLACE INTO bam_executions VALUES (?,?,?)",
                (record["preview_id"], status, json.dumps(record)),
            )

    def load_plan(self, preview_id, statuses):
        require(
            isinstance(preview_id, str) and re.fullmatch(r"[a-f0-9]{32}", preview_id),
            "Invalid execution ID",
            "invalid_preview",
        )
        with self.db() as db:
            self.table(db)
            row = db.execute(
                "SELECT status,record FROM bam_executions WHERE id=?", (preview_id,)
            ).fetchone()
        require(
            row and row[0] in statuses,
            "Execution plan absent or consumed; do not retry",
            "preview_not_ready",
        )
        return json.loads(row[1])

    def checkpoint(self, checkpoint_id, request, expected=None):
        service = Checkpoints(self.state)
        root, policy = service.scope()
        require(
            policy == request["policy_sha256"]
            and str(root) == request["policy"]["fixture"],
            "Checkpoint scope changed",
            "checkpoint_scope_denied",
        )
        value, _ = service.load(checkpoint_id, root, policy)
        digest = value["inventory"]["digest"]
        require(
            expected is None or digest == expected,
            "Checkpoint changed after approval",
            "checkpoint_corrupt",
        )
        return digest

    def preview_execution(self, session_id, impact_id, checkpoint_id):
        with self.lock():
            request = self.base(session_id, "CORPORATE", "Powersupply", "bam_plan")
            with self.db() as db:
                row = db.execute(
                    "SELECT status,record FROM backannotation_impacts WHERE id=?",
                    (impact_id,),
                ).fetchone()
            require(
                row and row[0] == "inspected",
                "Require a successful scoped impact",
                "check_not_found",
            )
            impact = json.loads(row[1])
            self.same_scope(impact, request)
            require(
                impact["design"] == "CORPORATE"
                and impact.get("scope_sheet") == "Powersupply"
                and impact["mode"] == "repackage_unfixed"
                and impact["repeatability"]["identical"],
                "Require a repeatable scoped impact",
            )
            checkpoint_digest = self.checkpoint(checkpoint_id, request)
            source = Path(impact["evidence"]["first"]["bam"])
            require(
                source.resolve()
                == (
                    self.state
                    / "evidence"
                    / ("backannotation-" + impact_id)
                    / "first/changes.bam"
                ).resolve(),
                "Unexpected impact file location",
            )
            digest = impact["evidence"]["first"]["bam_sha256"]
            records = read_approved(source, digest)
            require(
                records == impact["proposal_records"],
                "Historical impact differs from its BAM",
                "approved_bam_changed",
            )
            preview_id = uuid.uuid4().hex
            folder = self.state / "evidence" / ("bam-execution-" + preview_id)
            folder.mkdir(parents=True, exist_ok=False)
            approved = folder / "approved.bam"
            data = source.read_bytes()
            require(
                hashlib.sha256(data).hexdigest() == digest,
                "Source BAM changed during copying",
                "approved_bam_changed",
            )
            approved.write_bytes(data)
            request.update(
                approved_file=str(approved.resolve()),
                approved_sha256=digest,
                records=records,
                impact_live=impact["live_baseline"],
                impact_graph=impact["netlist_baseline"],
                impact_provenance=impact["provenance"],
                output_dir=str(folder / "preview"),
            )
            result = self.checked(self.runner(request))
            self.sessions.load(session_id)
            record = {
                "preview_id": preview_id,
                "session_id": session_id,
                "impact_id": impact_id,
                "checkpoint_id": checkpoint_id,
                "checkpoint_digest": checkpoint_digest,
                "approved_file": request["approved_file"],
                "approved_sha256": digest,
                "records": records,
                "project": request["expected_path"],
                "policy_sha256": request["policy_sha256"],
                "expires_at": self.clock() + 600,
                "executable_preview": True,
                "execution_scope": "three_fixture_instances_reference_and_same_value_part_overrides",
                "recovery": "Close owned editors and explicitly restore the complete recorded checkpoint; no automatic inverse.",
                **result,
            }
            self.save(record, "ready")
            return record

    def run_execution(self, preview_id, session_id=None, reopen=False):
        with self.lock():
            record = self.load_plan(preview_id, ("executed",) if reopen else ("ready",))
            sid = session_id if reopen else record["session_id"]
            if not reopen and self.clock() > record["expires_at"]:
                self.save(record, "expired")
                raise PadsError("preview_expired", "Create a new execution preview")
            request = self.base(
                sid, "CORPORATE", "Powersupply", "bam_reopen" if reopen else "bam_apply"
            )
            self.same_scope(record, request)
            try:
                self.checkpoint(
                    record["checkpoint_id"], request, record["checkpoint_digest"]
                )
                require(
                    read_approved(record["approved_file"], record["approved_sha256"])
                    == record["records"],
                    "Approved file changed",
                    "approved_bam_changed",
                )
            except Exception:
                self.save(record, "invalidated")
                raise
            request.update(
                {
                    k: record[k]
                    for k in (
                        "before",
                        "after",
                        "protected",
                        "provenance",
                        "approved_file",
                        "approved_sha256",
                        "records",
                        "expires_at",
                    )
                }
            )
            request["output_dir"] = str(
                Path(record["approved_file"]).parent
                / ("reopen" if reopen else "execute")
            )
            self.save(record, "reopening" if reopen else "executing")
            try:
                result = self.runner(request)
            except Exception:
                self.save(record, "outcome_unknown")
                self.sessions.invalidate(sid, "bam_outcome_unknown")
                raise
            record["result"] = result
            if result.get("ok") is not True:
                self.save(record, "failed")
                if result.get("project_write_attempted") is not False or result.get(
                    "lifecycle_attempted"
                ):
                    self.sessions.invalidate(sid, "bam_needs_inspection")
                return {"preview_id": preview_id, **result}
            if result.get("observed") != record["after"]:
                self.save(record, "readback_mismatch")
                self.sessions.invalidate(sid, "bam_readback_mismatch")
                raise PadsError(
                    "bam_readback_mismatch", "Result does not match approved plan"
                )
            self.sessions.invalidate(
                sid, "bam_reopened" if reopen else "bam_applied_rebind"
            )
            self.save(record, "verified" if reopen else "executed")
            return {
                "preview_id": preview_id,
                "status": "persistence_verified" if reopen else "executed",
                **result,
            }
