"""Read native pin positions and wire endpoints without inferred unit conversion."""

from .native_designer import call
from .geometry import canonical
from contextlib import contextmanager
from .sessions import PadsError


@contextmanager
def exact_grid(app):
    """Use a fine raw VDVAL_GRID value, then restore the exact native setting.

    The raw parameter scale is not the component coordinate unit. Acceptance
    checks exact requested coordinates separately rather than inferring a scale.
    """
    previous = int(call(app, "ParamGetValue", 0))
    try:
        call(app, "ParamSetValue", 0, 1)
        if int(call(app, "ParamGetValue", 0)) != 1:
            raise PadsError(
                "grid_not_applied", "Cannot establish exact 0.01-inch coordinates"
            )
        yield
    finally:
        call(app, "ParamSetValue", 0, previous)
        if int(call(app, "ParamGetValue", 0)) != previous:
            raise PadsError(
                "grid_restore_failed", "Test editor grid setting could not be restored"
            )


def point(obj):
    return [int(obj.X), int(obj.Y)]


def net_state(net):
    segments = []
    for s in call(net, "GetSegments"):
        label = call(net, "GetLabel", s)
        decoration = {}
        if label:
            decoration["label"] = {
                "text": str(label.TextString),
                "resolved_name": str(label.ResolvedName),
                "scope": int(label.Scope),
                "point": point(call(label, "GetLocation")),
                "visible": bool(label.Visible),
                "orientation": int(label.Orientation),
                "size": int(label.Size),
                "sense": int(label.Sense),
                "origin": int(label.Origin),
                "font": int(label.Font),
            }
        attributes = [
            {
                "name": str(a.Name),
                "base_value": str(a.Value),
                "instance_value": str(a.InstanceValue),
                "effective_value": str(a.EitherValue),
            }
            for a in s.Attributes
        ]
        if attributes:
            decoration["attributes"] = canonical(attributes)
        segments.append(
            {
                "a": point(call(s, "Location", 0)),
                "b": point(call(s, "Location", 1)),
                "joint_a": int(call(s, "GetJointType", 0)),
                "joint_b": int(call(s, "GetJointType", 1)),
                "bus": bool(call(s, "IsBus")),
                "decoration_checked": True,
                **({"decoration": decoration} if decoration else {}),
            }
        )
    return {
        "uid": str(net.UID),
        "name": str(net.LogicalNetName),
        "segments": canonical(segments),
    }


def component_geometry(item, wires=True):
    pins, nets = [], {}
    for c in call(item, "GetConnections"):
        cp, net = c.CompPin, c.Net
        pins.append(
            {
                "uid": str(cp.Pin.UID),
                "number": str(cp.Number),
                "name": str(call(cp.Pin, "GetName", 1)),
                "point": point(call(cp, "GetLocation")),
                "net_uid": str(net.UID) if net else None,
            }
        )
        if net and wires:
            nets[str(net.UID)] = net_state(net)
    result = {
        "pins": canonical(pins),
        "bbox": [
            point(call(item, "GetBboxPoint", 0)),
            point(call(item, "GetBboxPoint", 1)),
        ],
    }
    if wires:
        result["wire_geometry"] = canonical(list(nets.values()))
    return result
