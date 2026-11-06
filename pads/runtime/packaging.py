"""Single-use, identity-preserving packaging of one recorded reused placement."""

import copy
import json
from pathlib import Path
import re
import uuid

from .logical import validate_pins
from .diagnostics import fingerprint
from .schematic import Schematic
from .sessions import PadsError


def expected_graph(baseline, before, placed):
    old = {c["target"]["uid"] for c in before["components"]}
    added = [c for c in placed["components"] if c["target"]["uid"] not in old]
    if len(added) != 1:
        raise PadsError(
            "packaging_scope_denied", "Require exactly one recorded placement"
        )
    component = added[0]
    ref = component["target"]["refdes"]
    pins = [{"refdes": ref, "pin_number": p["number"]} for p in component["pins"]]
    validate_pins(pins)
    if (
        any(p["net_uid"] for p in component["pins"])
        or "\\" in component["target"]["path"]
    ):
        raise PadsError(
            "packaging_scope_denied",
            "Only one unconnected root-sheet instance is supported",
        )
    graph = copy.deepcopy(baseline["netlist"])
    if ref in {c["refdes"] for c in graph["components"]}:
        raise PadsError(
            "packaging_scope_denied",
            "Baseline must precede the new reference designator",
        )
    graph["components"].append(
        {
            "refdes": ref,
            "pin_numbers": sorted(p["pin_number"] for p in pins),
            "repeated_pin_declarations": [],
        }
    )
    graph["components"].sort(key=lambda c: c["refdes"])
    return graph, pins


def validate_output(returncode, stdout, stderr, log, bam, preview):
    text = stdout + "\n" + stderr + "\n" + log
    required = [
        "Packaging has been successfully tested with no errors or warnings.",
        "Packager finished successfully.",
    ]
    required.append(
        "!THE iCDB HAS NOT BEEN UPDATED!" if preview else "!THE iCDB IS UP-TO-DATE!"
    )
    if (
        returncode != 0
        or stderr.strip()
        or any(s not in text for s in required)
        or re.search(r"(?im)^\s*(?:ERROR|WARNING)\b", text)
    ):
        raise PadsError(
            "packaging_diagnostics",
            "Native packaging did not complete cleanly; inspect retained logs",
        )
    if preview and "The Common DataBase has been successfully updated." in text:
        raise PadsError(
            "packaging_unexpected_write",
            "Preview reported an unexpected database update",
        )
    # This first increment never applies a rename, pin reassignment or nonempty BAM.
    if bam != b"":
        raise PadsError(
            "packaging_assignment_required",
            "Native proposal contains assignments; renumbering/pin remapping is not enabled",
        )


class Packaging(Schematic):
    def load_packaging(self, preview_id):
        if not isinstance(preview_id, str) or not re.fullmatch(
            "[a-f0-9]{32}", preview_id
        ):
            raise PadsError("invalid_preview", "Invalid packaging preview ID")
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS packaging (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT status,record FROM packaging WHERE id=?", (preview_id,)
            ).fetchone()
        if row is None:
            raise PadsError("preview_not_ready", "Packaging preview was not found")
        return row[0], json.loads(row[1])

    def save_packaging(self, record, status):
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS packaging (id TEXT PRIMARY KEY,status TEXT NOT NULL,record TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO packaging VALUES (?,?,?)",
                (record["preview_id"], status, json.dumps(record)),
            )

    def placement(self, change_id):
        status, previous = self.previous(change_id)
        if (
            status not in ("executed", "verified")
            or previous["action"] != "execute"
            or previous["plan"]["kind"] != "place"
            or not previous.get("result", {}).get("ok")
        ):
            raise PadsError(
                "packaging_scope_denied",
                "Require a successful unrecovered reused-placement record",
            )
        return previous

    def preview_packaging(self, session_id, change_id, baseline_id):
        with self.lock():
            previous = self.placement(change_id)
            request = self.base(
                session_id, previous["design"], previous["sheet"], "packaging_preview"
            )
            self.same_scope(previous, request)
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS logical_checks (id TEXT PRIMARY KEY,record TEXT NOT NULL)"
                )
                row = db.execute(
                    "SELECT record FROM logical_checks WHERE id=?", (baseline_id,)
                ).fetchone()
            if row is None:
                raise PadsError(
                    "check_not_found",
                    "Require a successful logical baseline before placement",
                )
            baseline = json.loads(row[0])
            self.same_scope(baseline, request)
            if baseline["design"] != previous["design"]:
                raise PadsError("check_scope_mismatch", "Baseline design differs")
            expected, pins = expected_graph(
                baseline, previous["plan"]["before"], previous["result"]["sheet"]
            )
            preview_id = uuid.uuid4().hex
            output = str(
                (self.state / "evidence" / ("packaging-" + preview_id)).resolve()
            )
            request.update(
                expected_sheet=previous["result"]["sheet"],
                expected_graph=expected,
                pins=pins,
                output_dir=output,
                baseline_provenance=baseline["provenance"],
            )
            try:
                result = self.runner(request)
            except Exception:
                self.save_packaging(
                    {"preview_id": preview_id, "request": request}, "outcome_unknown"
                )
                self.sessions.invalidate(session_id, "packaging_preflight_unknown")
                raise
            if result.get("ok") is not True:
                self.save_packaging(
                    {"preview_id": preview_id, "request": request, "result": result},
                    "failed",
                )
                if result.get("schematic_update_outcome_unknown"):
                    self.sessions.invalidate(session_id, "packaging_preflight_unknown")
                self.checked(result)
            self.sessions.load(session_id)
            record = {
                "preview_id": preview_id,
                "session_id": session_id,
                "change_id": change_id,
                "baseline_id": baseline_id,
                "design": previous["design"],
                "sheet": previous["sheet"],
                "project": request["expected_path"],
                "policy_sha256": request["policy_sha256"],
                "expires_at": self.clock() + 600,
                "request": request,
                "preview": result,
                "scope": "one_unconnected_explicitly_numbered_reused_placement_empty_bam_only",
                "recovery": "Remove this recorded unconnected placement with the existing guarded recovery tools; this restores schematic semantics, not byte-identical packager bookkeeping.",
                "auxiliary_files_written": True,
                "schematic_update_attempted": False,
            }
            self.save_packaging(record, "ready")
            return record

    def execute_packaging(self, preview_id):
        with self.lock():
            status, record = self.load_packaging(preview_id)
            if status != "ready":
                raise PadsError(
                    "preview_not_ready", "Packaging preview is single-use; do not retry"
                )
            if self.clock() > record["expires_at"]:
                self.save_packaging(record, "expired")
                raise PadsError("preview_expired", "Create a new packaging preview")
            previous = self.placement(record["change_id"])
            request = self.base(
                record["session_id"],
                record["design"],
                record["sheet"],
                "packaging_execute",
            )
            self.same_scope(record, request)
            if previous["result"]["sheet"] != record["request"]["expected_sheet"]:
                raise PadsError("stale_preview", "Recorded placement changed")
            request.update(
                {
                    k: record["request"][k]
                    for k in (
                        "expected_sheet",
                        "expected_graph",
                        "pins",
                        "output_dir",
                        "baseline_provenance",
                    )
                }
            )
            request.update(
                expected_preview=record["preview"], expires_at=record["expires_at"]
            )
            self.save_packaging(record, "executing")
            try:
                result = self.runner(request)
            except Exception:
                self.save_packaging(record, "outcome_unknown")
                self.sessions.invalidate(
                    record["session_id"], "packaging_outcome_unknown"
                )
                raise
            record["result"] = result
            self.save_packaging(
                record, "executed" if result.get("ok") is True else "failed"
            )
            if not result.get("ok"):
                if result.get("schematic_update_attempted") is not False or result.get(
                    "schematic_update_outcome_unknown"
                ):
                    self.sessions.invalidate(
                        record["session_id"], "packaging_needs_inspection"
                    )
                return {"preview_id": preview_id, **result}
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
            return {
                "preview_id": preview_id,
                "status": "executed",
                "placement_change_id": record["change_id"],
                **result,
            }
