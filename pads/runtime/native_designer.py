"""Attach-only Designer reader. Uses native UIDs, never document editing APIs."""
import json
import os
from pathlib import Path
import site
import sys

ROOT = Path(__file__).resolve().parents[2]
site.addsitedir(str(ROOT / '.pads-runtime/python'))
sys.path.insert(0, str(ROOT / 'pads'))
from runtime.native_layout import process_identity
from runtime.sessions import PadsError


def read(request):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    table = pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    expected = request.get('expected_identity')
    prefix = '!Viewdraw.PID '
    candidates = [m for m in table.EnumRunning() if m.GetDisplayName(ctx, None).startswith(prefix)
                  and (not expected or m.GetDisplayName(ctx, None) == prefix + str(expected['pid']))]
    if not candidates:
        raise PadsError('application_not_running', 'No accessible Designer instance')
    if len(candidates) != 1:
        raise PadsError('application_ambiguous', 'Multiple Designer instances; leave one accessible instance before binding')
    app = win32com.client.Dispatch(table.GetObject(candidates[0]).QueryInterface(pythoncom.IID_IDispatch))
    def call(obj, name, *args):
        # VX.2.4 marks some zero-argument methods inconsistently for late binding.
        value = obj._oleobj_.Invoke(obj._oleobj_.GetIDsOfNames(name), 0, pythoncom.DISPATCH_METHOD, True, *args)
        if isinstance(value, pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
            return win32com.client.Dispatch(value)
        return value

    def identity():
        handle = int(app.ActiveViewHandle) & 0xffffffff
        if not handle:
            raise PadsError('document_not_ready', 'Open an existing schematic sheet before binding')
        process = process_identity(handle)
        if not call(app, 'IsProjectOpened'):
            raise PadsError('no_project', 'No project is open')
        path = str(Path(call(call(app, 'GetProjectData'), 'GetProjectFilePath')).resolve(strict=True))
        if os.path.normcase(path) != os.path.normcase(request['expected_path']):
            raise PadsError('target_mismatch', 'Active project differs from the requested path')
        result = {**process,'domain':'designer','instance_guid':str(call(app, 'Guid')),'project_path':path}
        if expected and any(str(result[k]) != str(expected[k]) for k in ('instance_guid','pid','process_started')):
            raise PadsError('session_disconnected', 'Application instance changed; bind a new session')
        return result

    before = identity()
    inventory = None
    if request['inventory']:
        pages, components, nets = [], [], []
        designs = call(call(app, 'GetProjectData'), 'GetiCDBDesigns')
        for number in range(1, call(designs, 'GetCount')+1):
            design = call(designs, 'GetItem', number)
            for sheet in call(app, 'QueryPages', design):
                pages.append({'design':design,'name':str(call(sheet, 'GetName', 1)),'sheet':str(sheet.SheetNum)})
            for item in call(app, 'DesignComponents', '', design, '-1', '', True):
                uid = item.UID
                if not uid:
                    raise PadsError('missing_object_identity', 'Component has no native UID')
                components.append({'design':design,'uid':str(uid),'path':str(call(item, 'GetName', 3)),
                                   'refdes':str(item.Refdes),'sheet':str(item.Parent.SheetNum)})
                if len(components) > 100000:
                    raise PadsError('inventory_too_large', 'Component limit exceeded')
            for item in call(app, 'DesignNets', '', design, '-1', '', True):
                uid = item.UID
                if not uid:
                    raise PadsError('missing_object_identity', 'Net has no native UID')
                nets.append({'design':design,'uid':str(uid),'name':str(item.LogicalNetName),'sheet':str(item.Parent.SheetNum)})
                if len(nets) > 100000:
                    raise PadsError('inventory_too_large', 'Net limit exceeded')
        inventory = {'pages':pages,'components':components,'nets':nets}
    after = identity()
    if before != after:
        raise PadsError('target_changed_during_read', 'Application or project changed while reading')
    return {'ok':True,'identity':after,'inventory':inventory,'project_write_attempted':False}


if __name__ == '__main__':
    try:
        print(json.dumps(read(json.loads(sys.stdin.read(1024*1024))),ensure_ascii=False))
    except PadsError as exc:
        print(json.dumps({'ok':False,'error':{'kind':exc.kind,'message':str(exc)}}))
    except Exception:
        print(json.dumps({'ok':False,'error':{'kind':'native_read_failed','message':'Designer read failed; no design write was requested'}}))
