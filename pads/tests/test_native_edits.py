"""Failure injection around the native write boundary; no editor activation."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import native_edit
from runtime.edits import proposed_state


@pytest.fixture
def native_context(tmp_path, monkeypatch):
    current = {'attributes': [{'name': 'Description', 'base_value': 'base', 'instance_value': '', 'effective_value': 'base'}],
               'position': {'x': 10, 'y': 10, 'unit': '0.01_inch'}, 'connections': [{'pin_uid': 'P1', 'net_uid': 'N1'}]}
    before = copy.deepcopy(current)
    writes = []
    class Attribute:
        Name = 'Description'
        @property
        def InstanceValue(self):
            return current['attributes'][0]['instance_value']
        @InstanceValue.setter
        def InstanceValue(self, value):
            writes.append('attribute')
            current['attributes'][0].update(instance_value=value, effective_value=value)
    attr = Attribute()
    def move(x, y):
        writes.append('position')
        current['position'].update(x=x, y=y)
    item = SimpleNamespace(Parent=SimpleNamespace(IsReadOnly=lambda: False),
                           FindAttribute=lambda name: attr, SetLocation=move)
    app = SimpleNamespace(GetProjectData=lambda: SimpleNamespace(iCDBDir=str(tmp_path), CentralLibraryPath=str(tmp_path)),
                          IsReadOnlyMode=lambda: False)
    grid=[10]
    app.ParamGetValue=lambda index:grid[0]
    app.ParamSetValue=lambda index,value:grid.__setitem__(0,value)
    monkeypatch.setattr(native_edit, 'check_fixture', lambda policy, path: tmp_path)
    monkeypatch.setattr(native_edit, 'connect', lambda request: (app, lambda: {'identity': 'fixed'}))
    monkeypatch.setattr(native_edit, 'locate', lambda app, target: item)
    monkeypatch.setattr(native_edit, 'component_state', lambda item, target: copy.deepcopy(current))
    monkeypatch.setattr(native_edit, 'call', lambda obj, name, *args: getattr(obj, name)(*args))
    from runtime import native_geometry
    monkeypatch.setattr(native_geometry, 'call', lambda obj, name, *args: getattr(obj, name)(*args))
    changes = {'attribute': {'name': 'Description', 'value': 'changed'},
               'position': {'x': 20, 'y': 20, 'unit': '0.01_inch'}}
    request = {'action': 'execute', 'com_version': '66', 'policy': {}, 'expected_path': 'unused', 'target': {},
               'before': before, 'after': proposed_state(before, changes), 'changes': changes, 'expires_at': 9999999999}
    return request, current, writes, item, app


def test_native_stale_and_read_only_checks_precede_setters(native_context):
    request, current, writes, item, _ = native_context
    current['connections'] = []
    result = native_edit.operate(request)
    assert result['error']['kind'] == 'stale_preview' and result['project_write_attempted'] is False
    current.update(copy.deepcopy(request['before']))
    item.Parent.IsReadOnly = lambda: True
    assert native_edit.operate(request)['error']['kind'] == 'document_read_only'
    assert writes == []


def test_partial_setter_failure_reports_write_attempt(native_context):
    request, current, writes, item, _ = native_context
    def fail(*args):
        raise RuntimeError('native position failure')
    item.SetLocation = fail
    result = native_edit.operate(request)
    assert result['error']['kind'] == 'native_edit_failed' and result['project_write_attempted'] is True
    assert writes == ['attribute'] and current['attributes'][0]['instance_value'] == 'changed'


def test_expired_preview_is_refused_at_native_boundary(native_context):
    request, _, writes, _, _ = native_context
    request['expires_at'] = 0
    result = native_edit.operate(request)
    assert result['error']['kind'] == 'preview_expired' and result['project_write_attempted'] is False
    assert writes == []


def test_connection_change_is_not_reported_as_success(native_context):
    request, current, writes, item, _ = native_context
    original_move = item.SetLocation
    def disconnect(x, y):
        original_move(x, y)
        current['connections'] = []
    item.SetLocation = disconnect
    result = native_edit.operate(request)
    assert result['error']['kind'] == 'readback_mismatch' and result['project_write_attempted'] is True
    assert result['component']['connections'] == []


def test_invalid_approved_state_and_external_database_are_refused(native_context, tmp_path):
    request, _, writes, _, app = native_context
    request['after']['connections'] = []
    assert native_edit.operate(request)['error']['kind'] == 'invalid_preview'
    app.GetProjectData = lambda: SimpleNamespace(iCDBDir=str(tmp_path.parent), CentralLibraryPath=str(tmp_path))
    assert native_edit.operate(request)['error']['kind'] == 'write_scope_denied'
    assert writes == []


def test_raw_invoke_unwraps_native_pin_arguments():
    from runtime.native_designer import call
    received=[]
    class Ole:
        def GetIDsOfNames(self,name):return 7
        def Invoke(self,*args):
            received.append(args)
            return 'native-result'
    obj=SimpleNamespace(_oleobj_=Ole())
    native_pin=object()
    pin=SimpleNamespace(_oleobj_=native_pin)
    assert call(obj,'AddNet',0,pin)=='native-result'
    assert received[0][-1] is native_pin
