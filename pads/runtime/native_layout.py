"""Layout reads through pywin32; only its in-process licensing bridge is 32-bit."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import site
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
site.addsitedir(str(ROOT / '.pads-runtime/python'))
sys.path.insert(0, str(ROOT / 'pads'))
from runtime.sessions import PadsError


def process_identity(hwnd):
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.IsWindowEnabled.argtypes = [wintypes.HWND]
    user.IsHungAppWindow.argtypes = [wintypes.HWND]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        raise PadsError('identity_unverified', 'Application window is unavailable')
    root = user.GetAncestor(hwnd, 2)
    if not root or not user.IsWindowEnabled(root) or user.IsHungAppWindow(root):
        raise PadsError('application_busy_or_modal', 'Application window is blocked')
    process = kernel.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise PadsError('identity_unverified', 'Cannot inspect the native process')
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Same .NET UTC tick convention as the Designer adapter (1601 -> year 1).
        ticks = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime + 504911232000000000
        return {'pid': pid.value, 'process_started': str(ticks), 'executable': path.value}
    finally:
        kernel.CloseHandle(process)


def read(request):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    table = pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    expected = request.get('expected_identity')
    prefix = '!ExpeditionPEApplication:'
    candidates = [m for m in table.EnumRunning() if m.GetDisplayName(ctx, None).startswith(prefix)
                  and (not expected or m.GetDisplayName(ctx, None) == prefix + str(expected['pid']))]
    if not candidates:
        raise PadsError('application_not_running', 'No accessible Layout instance')
    if len(candidates) != 1:
        raise PadsError('application_ambiguous', 'Multiple Layout instances; leave one accessible instance before binding')
    app = win32com.client.Dispatch(table.GetObject(candidates[0]).QueryInterface(pythoncom.IID_IDispatch))

    def identity():
        process = process_identity(int(app.Gui.HWND) & 0xffffffff)
        if not app.IsDocumentReady:
            raise PadsError('document_not_ready', 'PCB document is not ready')
        if app.IsDBLocked:
            raise PadsError('application_busy_or_modal', 'PCB database is busy')
        path = str(Path(app.ActiveDocument.FullName).resolve(strict=True))
        if os.path.normcase(path) != os.path.normcase(request['expected_path']):
            raise PadsError('target_mismatch', 'Active project differs from the requested path')
        result = {**process, 'domain':'layout', 'instance_guid':str(app.InstanceGuid), 'project_path':path}
        if expected and any(str(result[k]) != str(expected[k]) for k in ('instance_guid','pid','process_started')):
            raise PadsError('session_disconnected', 'Application instance changed; bind a new session')
        return result

    before = identity()
    inventory = None
    if request['inventory']:
        doc = app.ActiveDocument
        # The licensing COM DLL is x86. Exchange its challenge/token only in memory.
        helper = subprocess.run([str(Path(os.environ['WINDIR'])/'SysWOW64/WindowsPowerShell/v1.0/powershell.exe'),
                                 '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
                                 str(Path(__file__).with_name('license_token.ps1'))],
                                input=json.dumps({'key':doc.Validate(0)}).encode('utf-8'),
                                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if helper.returncode:
            raise PadsError('document_license_failed', 'Native automation licensing handshake failed')
        token = json.loads(helper.stdout)['token']
        doc.Validate(token)
        del token, helper
        components, nets = [], []
        for item in doc.Components:
            uid = str(item.UniqueId)
            if not uid:
                raise PadsError('missing_object_identity', 'PCB component has no native UID')
            components.append({'uid':uid,'refdes':str(item.RefDes),'part':str(item.PartName),
                               'cell':str(item.CellName),'placed':bool(item.Placed)})
            if len(components) > 100000:
                raise PadsError('inventory_too_large', 'Component limit exceeded')
        for item in doc.Nets:
            uid = str(item.UniqueId)
            if not uid:
                raise PadsError('missing_object_identity', 'PCB net has no native UID')
            nets.append({'uid':uid,'name':str(item.Name),'pin_count':int(item.Pins.Count)})
            if len(nets) > 100000:
                raise PadsError('inventory_too_large', 'Net limit exceeded')
        inventory = {'pages':[], 'components':components, 'nets':nets}
    after = identity()
    if before != after:
        raise PadsError('target_changed_during_read', 'Application or project changed while reading')
    return {'ok':True, 'identity':after, 'inventory':inventory, 'project_write_attempted':False}


if __name__ == '__main__':
    try:
        print(json.dumps(read(json.loads(sys.stdin.read(1024*1024))), ensure_ascii=False))
    except PadsError as exc:
        print(json.dumps({'ok':False,'error':{'kind':exc.kind,'message':str(exc)}}))
    except Exception:
        # Do not include licensing parameters or private helper output in errors.
        print(json.dumps({'ok':False,'error':{'kind':'native_read_failed','message':'Layout read failed; no design write was requested'}}))
