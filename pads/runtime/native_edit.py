"""Narrow Designer edit adapter: no arbitrary COM setters, activation or library edits."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pads'))
from runtime.native_designer import connect, call
from runtime.edits import check_fixture, proposed_state, validate_target
from runtime.sessions import PadsError


def component_state(item, target, geometry=True):
    point = call(item, 'GetLocation')
    attributes = [{'name': str(a.Name), 'base_value': str(a.Value),
                   'instance_value': str(a.InstanceValue), 'effective_value': str(a.EitherValue)}
                  for a in item.Attributes]
    connections = []
    for connection in call(item, 'GetConnections'):
        pin, net = connection.CompPin.Pin, connection.Net
        connections.append({'pin_uid': str(pin.UID), 'pin_name': str(call(pin, 'GetName', 1)),
                            'net_uid': str(net.UID) if net else None,
                            'net_name': str(net.LogicalNetName) if net else None})
    result = {'target': target, 'symbol': str(call(item.SymbolBlock, 'GetName', 0)),
            'position': {'x': int(point.X), 'y': int(point.Y), 'unit': '0.01_inch'},
            'orientation': int(item.Orientation),
            'attributes': sorted(attributes, key=lambda a: (a['name'], a['base_value'], a['instance_value'])),
            'connections': sorted(connections, key=lambda c: json.dumps(c, sort_keys=True))}
    if geometry:
        from runtime.native_geometry import component_geometry
        result.update(component_geometry(item))
    return result


def locate(app, target):
    validate_target(target)
    matches = []
    for item in call(app, 'DesignComponents', '', target['design'], '-1', '', True):
        if (str(item.UID) == target['uid'] and str(item.Refdes) == target['refdes']
                and str(item.Parent.SheetNum) == target['sheet'] and str(call(item, 'GetName', 3)) == target['path']):
            matches.append(item)
    if len(matches) != 1:
        raise PadsError('component_not_unique', 'Exact existing component identity was not uniquely resolved')
    return matches[0]


def operate(request):
    attempted = False
    lifecycle_attempted = False
    try:
        if request.get('com_version') != '66':
            raise PadsError('unsupported_version', 'P2 edits are verified only for COM version 66')
        root = check_fixture(request['policy'], request['expected_path'])
        app, identity = connect(request)
        before_identity = identity()
        project = call(app, 'GetProjectData')
        for path in (project.iCDBDir, project.CentralLibraryPath):
            if not Path(path).resolve(strict=True).is_relative_to(root):
                raise PadsError('write_scope_denied', 'Native database and library must both be inside the test copy')
        target = request['target']
        item = locate(app, target)
        current = component_state(item, target)
        if identity() != before_identity:
            raise PadsError('target_changed', 'Target changed during component inspection')
        action = request['action']
        if action == 'inspect':
            return {'ok': True, 'identity': before_identity, 'component': current, 'project_write_attempted': False}
        if current != request['before']:
            raise PadsError('stale_preview', 'Component attributes, position or pin/net identities changed after preview')
        if call(app, 'IsReadOnlyMode') or call(item.Parent, 'IsReadOnly'):
            raise PadsError('document_read_only', 'The component is in a read-only document')
        if action == 'execute':
            changes = request['changes']
            if proposed_state(current, changes) != request['after']:
                raise PadsError('invalid_preview', 'Desired state does not match the bounded change')
            attr = None
            if 'attribute' in changes:
                attr = call(item, 'FindAttribute', changes['attribute']['name'])
                if attr is None or str(attr.Name) != changes['attribute']['name']:
                    raise PadsError('attribute_not_unique', 'Existing attribute could not be resolved')
            if identity() != before_identity:
                raise PadsError('target_changed', 'Target changed immediately before execution')
            if time.time() > request['expires_at']:
                raise PadsError('preview_expired', 'Preview expired before the native write boundary')
            # No claim of ACID or automatic rollback: a setter may fail after changing native state.
            attempted = True
            if attr is not None:
                attr.InstanceValue = changes['attribute']['value']
                if identity() != before_identity:
                    raise PadsError('target_changed', 'Target changed after attribute write; position was not modified')
            if 'position' in changes:
                pos = changes['position']
                from runtime.native_geometry import exact_grid
                with exact_grid(app):
                    call(item, 'SetLocation', pos['x'], pos['y'])
            actual = component_state(item, target)
        elif action == 'reopen':
            # Schematic edits are stored immediately in iCDB. Sheet.Save is ESE-only.
            docs = call(app, 'SchematicSheetDocuments')
            sheets = call(docs, 'GetAvailableSheets', target['design'])
            names = [str(call(sheets, 'GetItem', i)) for i in range(1, call(sheets, 'GetCount') + 1)]
            if target['sheet'] not in names:
                raise PadsError('sheet_not_found', 'Existing sheet is required; never create a sheet while reopening')
            if identity() != before_identity:
                raise PadsError('target_changed', 'Target changed before close/reopen')
            lifecycle_attempted = True
            if not call(app, 'CloseProject'):
                raise PadsError('close_failed', 'Native project did not close')
            if call(app, 'IsProjectOpened'):
                raise PadsError('close_failed', 'A project is still open; refusing to replace it')
            call(app, 'OpenProject', request['expected_path'])
            path = Path(call(call(app, 'GetProjectData'), 'GetProjectFilePath')).resolve(strict=True)
            if path != Path(request['expected_path']).resolve(strict=True):
                raise PadsError('target_mismatch', 'Unexpected project after reopening')
            # Open only a sheet whose existence was checked both before and after reopen.
            docs = call(app, 'SchematicSheetDocuments')
            sheets = call(docs, 'GetAvailableSheets', target['design'])
            if target['sheet'] not in [str(call(sheets, 'GetItem', i)) for i in range(1, call(sheets, 'GetCount') + 1)]:
                raise PadsError('sheet_not_found', 'Sheet missing after reopen')
            call(docs, 'Open', target['design'], target['sheet'])
            actual = component_state(locate(app, target), target)
        else:
            raise PadsError('invalid_action', 'Unknown bounded edit action')
        if identity() != before_identity:
            raise PadsError('target_changed', 'Target changed while editing or reopening')
        if actual != request['after']:
            return {'ok': False, 'error': {'kind': 'readback_mismatch', 'message': 'Native readback differs from preview; inspect the test copy before further edits'},
                    'component': actual, 'project_write_attempted': attempted, 'lifecycle_attempted': lifecycle_attempted}
        return {'ok': True, 'identity': before_identity, 'component': actual,
                'project_write_attempted': attempted, 'lifecycle_attempted': lifecycle_attempted,
                'persistence': 'native_database_close_reopen_verified' if lifecycle_attempted else 'native_database_write_readback_verified'}
    except PadsError as exc:
        return {'ok': False, 'error': {'kind': exc.kind, 'message': str(exc)},
                'project_write_attempted': attempted, 'lifecycle_attempted': lifecycle_attempted}
    except Exception:
        return {'ok': False, 'error': {'kind': 'native_edit_failed', 'message': 'Native operation failed; inspect the test copy if a write or lifecycle action was attempted'},
                'project_write_attempted': attempted, 'lifecycle_attempted': lifecycle_attempted}


if __name__ == '__main__':
    print(json.dumps(operate(json.loads(sys.stdin.read(1024 * 1024))), ensure_ascii=False))
