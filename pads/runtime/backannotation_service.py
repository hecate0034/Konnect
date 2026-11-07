"""Historical impact records are kept apart from executable packaging previews."""

import json
import uuid

from .schematic import Schematic
from .sessions import PadsError


class Backannotation(Schematic):
    def save_impact(self, record, status):
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS backannotation_impacts (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO backannotation_impacts VALUES (?,?,?)",
                (record["impact_id"], status, json.dumps(record)),
            )

    def preview_impact(self, session_id, design, mode="add", baseline_id=None):
        if mode not in ("add", "repackage_unfixed"):
            raise PadsError(
                "invalid_backannotation_mode", "Select add or repackage_unfixed"
            )
        with self.lock():
            request = self.base(
                session_id, design, "*", "backannotation_preview", mode=mode
            )
            previous = None
            if baseline_id:
                with self.db() as db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS backannotation_impacts (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
                    )
                    row = db.execute(
                        "SELECT status,record FROM backannotation_impacts WHERE id=?",
                        (baseline_id,),
                    ).fetchone()
                if not row or row[0] != "inspected":
                    raise PadsError(
                        "check_not_found", "Require a successful impact baseline"
                    )
                previous = json.loads(row[1])
                self.same_scope(previous, request)
                if previous["design"] != design or previous["mode"] != mode:
                    raise PadsError(
                        "check_scope_mismatch", "Impact baseline design/mode differs"
                    )
            impact_id = uuid.uuid4().hex
            request["output_dir"] = str(
                (self.state / "evidence" / ("backannotation-" + impact_id)).resolve()
            )
            record = {
                "impact_id": impact_id,
                "design": design,
                "mode": mode,
                "project": request["expected_path"],
                "policy_sha256": request["policy_sha256"],
                "request": request,
                "executable_preview": False,
            }
            try:
                result = self.runner(request)
            except Exception:
                self.save_impact(record, "outcome_unknown")
                self.sessions.invalidate(session_id, "backannotation_preflight_unknown")
                raise
            record.update(result)
            if result.get("ok") is not True:
                self.save_impact(record, "failed")
                if result.get("schematic_update_outcome_unknown"):
                    self.sessions.invalidate(
                        session_id, "backannotation_preflight_unknown"
                    )
                return {"impact_id": impact_id, **result}
            self.sessions.load(session_id)
            if previous:
                if previous["provenance"] != record["provenance"]:
                    self.save_impact(record, "source_changed")
                    raise PadsError(
                        "backannotation_baseline_changed",
                        "PDB or native tools changed; create a fresh baseline",
                    )
                record.update(
                    baseline_id=baseline_id,
                    comparison={
                        "impact_equal": previous["impact"] == record["impact"],
                        "live_equal": previous["live_baseline"]
                        == record["live_baseline"],
                        "netlist_equal": previous["netlist_baseline"]
                        == record["netlist_baseline"],
                    },
                )
            self.save_impact(record, "inspected")
            return record
