"""Native batch attribute/OAT presence, including explicit empty overrides."""

import re

from .sessions import PadsError

FROZEN = "Frozen Package"


def parse_batch(text):
    if not isinstance(text, str) or len(text) > 256 * 1024 or "\x00" in text:
        raise PadsError(
            "attribute_state_unsupported", "Invalid batch attribute encoding"
        )
    values = {}
    for line in text.splitlines():
        if not line:
            continue
        match = re.fullmatch(r"([0-9]+) ([^=\r\n]+)=(.*)", line)
        if not match or match[2] in values:
            raise PadsError(
                "attribute_state_unsupported",
                "Malformed or duplicate native batch property",
            )
        values[match[2]] = {"visibility": int(match[1]), "value": match[3]}
    return values


def property_state(base, oats, name):
    regular, override = base.get(name), oats.get(name)
    return {
        "base_present": regular is not None,
        "base": regular["value"] if regular else None,
        "base_visibility": regular["visibility"] if regular else None,
        "instance_present": override is not None,
        "instance": override["value"] if override else None,
        "instance_visibility": override["visibility"] if override else None,
        "effective": (override or regular)["value"] if (override or regular) else None,
        "presence_verified": True,
    }


def scope_guard(live, design, sheet):
    if not isinstance(sheet, str) or not re.fullmatch(r"[A-Za-z0-9_ -]{1,80}", sheet):
        raise PadsError(
            "invalid_backannotation_scope", "Select an existing simple root sheet name"
        )
    scope = design + "(" + sheet + ")"
    selected = [r for r in live if r["scope"] == scope]
    outside = [r for r in live if r["scope"] != scope]
    if not 1 <= len(selected) <= 16:
        raise PadsError(
            "backannotation_scope_denied", "Require 1..16 existing root-sheet instances"
        )
    if any(not r.get("frozen", {}).get("presence_verified") for r in live):
        raise PadsError(
            "backannotation_scope_denied",
            "Native frozen-property preimages are required",
        )
    if any(r["frozen"]["effective"] != "Fix" for r in outside):
        raise PadsError(
            "backannotation_scope_not_frozen",
            "Prepare the disposable copy with every outside instance frozen; this preview never sets flags",
        )
    refs = {r["refdes"] for r in selected}
    if refs.intersection(r["refdes"] for r in outside):
        raise PadsError(
            "backannotation_scope_denied",
            "A selected package has units outside this sheet",
        )
    return scope
