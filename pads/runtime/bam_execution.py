"""Explicit, fail-closed BAM subset compiled to Designer COM operations.

This is not a vendor BAM importer. Only the three validated single-unit fixture
instances, fresh reference names, same-value part assignments and absent flag
deletions are supported. No pin changes, swaps, new parts or general scripting.
"""

import copy
import hashlib
import json
from pathlib import Path

from .backannotation import parse_bam
from .attribute_state import scope_guard
from .sessions import PadsError

SCOPE = "CORPORATE(Powersupply)"
TARGETS = {"$11I15": "C4", "$11I16": "C2", "$11I17": "U12"}
VERSION = "pads_native_bam_subset_v1"


def require(condition, message, kind="bam_scope_denied"):
    if not condition:
        raise PadsError(kind, message)


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted(
            (normalize(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
        )
    return value


def read_approved(path, digest):
    path = Path(path)
    require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_nlink == 1
        and path.stat().st_size <= 1024 * 1024,
        "Require an ordinary bounded approved file",
        "approved_bam_changed",
    )
    data = path.read_bytes()
    require(
        hashlib.sha256(data).hexdigest() == digest,
        "Approved BAM bytes changed",
        "approved_bam_changed",
    )
    return parse_bam(data.decode("mbcs", errors="strict"))


def planned(records, live, graph, sheet):
    require(
        len(records) == 3
        and {(r["scope"], r["uid"]) for r in records}
        == {(SCOPE, uid) for uid in TARGETS},
        "Only the three validated Powersupply instances are executable",
    )
    scope_guard(live, "CORPORATE", "Powersupply")
    lookup = {(r["scope"], r["uid"]): r for r in live}
    occupied = {r["refdes"] for r in live}
    changes = {}
    result = copy.deepcopy(live)
    rows = {(r["scope"], r["uid"]): r for r in result}
    for record in records:
        row = lookup.get((SCOPE, record["uid"]))
        ref = record["refdes"]
        require(
            row
            and ref
            and not record["pins"]
            and row["refdes"] == ref["before"] == TARGETS[record["uid"]],
            "Require unchanged known instances and no pin edits",
        )
        require(
            sum(r["refdes"] == row["refdes"] for r in live) == 1,
            "Shared/multi-unit packages are unsupported",
        )
        require(
            ref["after"] not in occupied
            and ref["after"] not in changes.values()
            and ref["after"][0] == ref["before"][0],
            "Destination reference must be unique, unused and retain its prefix",
        )
        require(
            record["delete_fixed"]
            and row["frozen"]["presence_verified"]
            and not row["frozen"]["base_present"]
            and not row["frozen"]["instance_present"],
            "Only deletion of an already absent fixed flag is supported",
        )
        require(
            set(record["attributes"]) == {"Part Name", "Part Label"},
            "Only native same-value part assignments are supported",
        )
        target = rows[(SCOPE, record["uid"])]
        for name, value in record["attributes"].items():
            old = row["attributes"][name]
            require(
                old["presence_verified"]
                and old["base_present"]
                and old["effective"] == old["base"] == value
                and not old["instance_present"]
                and old["base_visibility"] == 0,
                "Part assignments require verified, unchanged base values with no override",
            )
            target["attributes"][name].update(
                instance_present=True,
                instance=value,
                instance_visibility=0,
                effective=value,
            )
        target["refdes"] = ref["after"]
        changes[ref["before"]] = ref["after"]
    projected = copy.deepcopy(graph)
    for component in projected["components"]:
        component["refdes"] = changes.get(component["refdes"], component["refdes"])
    for net in projected["nets"]:
        for pin in net["pins"]:
            pin["refdes"] = changes.get(pin["refdes"], pin["refdes"])
    expected_sheet = copy.deepcopy(sheet)
    for component in expected_sheet["components"]:
        target = component["target"]
        if target["uid"] not in TARGETS:
            continue
        old_ref = TARGETS[target["uid"]]
        require(
            target["refdes"] == old_ref and target["path"] == target["uid"],
            "Sheet identity disagrees with instance identity",
        )
        target["refdes"] = changes[old_ref]
        for attr in component["attributes"]:
            if attr["name"] in ("Part Name", "Part Label"):
                attr["instance_value"] = attr["effective_value"]
            elif attr["name"] == "Ref Designator":
                attr.update(
                    instance_value=changes[old_ref], effective_value=changes[old_ref]
                )
    return {
        "live": normalize(result),
        "graph": normalize(projected),
        "sheet": normalize(expected_sheet),
    }
