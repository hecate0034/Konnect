import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configure_fixture_edits import configure
from runtime.edits import Edits, check_fixture, proposed_state
from runtime.sessions import Sessions, PadsError


TARGET = {'design': 'board', 'sheet': 'power', 'uid': 'I1', 'path': 'I1', 'refdes': 'R1'}
BEFORE = {'target': TARGET, 'symbol': 'resistor', 'position': {'x': 10, 'y': 20, 'unit': '0.01_inch'},
          'orientation': 0, 'connections': [{'pin_uid': 'P1', 'net_uid': 'N1'}],
          'attributes': [{'name': 'Description', 'base_value': 'resistor', 'instance_value': '', 'effective_value': 'resistor'}]}
CHANGE = {'attribute': {'name': 'Description', 'value': 'test resistor'},
          'position': {'x': 20, 'y': 30, 'unit': '0.01_inch'}}


@pytest.fixture
def context(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    state = tmp_path / 'state'
    fixture = state / 'test-copy'
    (fixture / 'LessonFinal').mkdir(parents=True)
    project = fixture / 'LessonFinal/LessonFinal.prj'
    project.write_bytes(b'test project')
    (fixture / 'source-manifest.json').write_text(json.dumps({'source': str(source)}))
    configure(state, fixture)
    sdd = tmp_path / 'sdd'
    sdd.mkdir()
    exe = sdd / 'viewdraw.exe'
    exe.write_bytes(b'test executable')
    identity = {'domain': 'designer', 'project_path': str(project), 'instance_guid': 'A',
                'pid': 42, 'process_started': '1000', 'executable': str(exe)}
    sessions = Sessions(sdd, state, lambda request: {'identity': identity})
    session = sessions.bind('designer', str(project))['session_id']
    current = copy.deepcopy(BEFORE)
    requests = []
    def runner(request):
        requests.append(request)
        if request['action'] != 'inspect':
            if request['before'] != current:
                return {'ok': False, 'error': {'kind': 'stale_preview', 'message': 'changed'}, 'project_write_attempted': False}
            current.clear()
            current.update(copy.deepcopy(request['after']))
        return {'ok': True, 'component': copy.deepcopy(current), 'project_write_attempted': request['action'] == 'execute'}
    service = Edits(sdd, state, sessions, runner, clock=lambda: 1000)
    return service, session, current, requests, fixture


def expect(kind, action):
    with pytest.raises(PadsError) as result:
        action()
    assert result.value.kind == kind


def test_preview_execute_readback_reopen_and_single_use(context):
    service, sid, current, requests, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    assert current == BEFORE and not preview['project_write_attempted']
    assert service.run(preview['preview_id'])['status'] == 'executed'
    assert current == preview['after']
    assert service.sessions.read(sid)['project_write_attempted'] is True
    expect('preview_not_ready', lambda: service.run(preview['preview_id']))
    assert service.run(preview['preview_id'], reopen=True)['status'] == 'persistence_verified'
    expect('session_invalidated', lambda: service.sessions.load(sid))
    expect('preview_not_ready', lambda: service.run(preview['preview_id'], reopen=True))


@pytest.mark.parametrize('change', [
    {}, {'arbitrary': 'call'}, {'attribute': {'name': 'Ref Designator', 'value': 'R2'}},
    {'attribute': {'name': 'Description', 'value': ''}}, {'attribute': {'name': 'Description', 'value': 'a\nb'}},
    {'position': {'x': True, 'y': 0, 'unit': '0.01_inch'}},
    {'position': {'x': 1.5, 'y': 0, 'unit': '0.01_inch'}},
    {'position': {'x': -1, 'y': 0, 'unit': '0.01_inch'}},
    {'position': {'x': 1, 'y': 0, 'unit': 'mm'}},
])
def test_invalid_changes_fail_before_native_access(context, change):
    service, sid, _, requests, _ = context
    expect('invalid_change', lambda: service.preview(sid, TARGET, change))
    assert requests == []


def test_missing_attribute_refused_and_base_values_preserved(context):
    service, sid, _, _, _ = context
    expect('attribute_not_unique', lambda: service.preview(sid, TARGET, {'attribute': {'name': 'Value', 'value': '10k'}}))
    result = proposed_state(BEFORE, CHANGE)
    assert result['attributes'][0]['base_value'] == 'resistor'
    assert result['connections'] == BEFORE['connections']


@pytest.mark.parametrize('field,value', [('orientation', 1), ('connections', []), ('symbol', 'capacitor')])
def test_stale_state_is_consumed_without_writing(context, field, value):
    service, sid, current, _, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    current[field] = value
    result = service.run(preview['preview_id'])
    assert result['error']['kind'] == 'stale_preview'
    assert result['project_write_attempted'] is False
    expect('preview_not_ready', lambda: service.run(preview['preview_id']))
    assert service.sessions.load(sid)


def test_expiry_and_released_binding_prevent_native_execution(context):
    service, sid, _, requests, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    service.clock = lambda: 1601
    expect('preview_expired', lambda: service.run(preview['preview_id']))
    assert len(requests) == 1
    service.clock = lambda: 1000
    preview = service.preview(sid, TARGET, CHANGE)
    service.sessions.release(sid)
    expect('session_invalidated', lambda: service.run(preview['preview_id']))
    assert len(requests) == 2


def test_worker_timeout_consumes_preview_and_invalidates_binding(context):
    service, sid, _, _, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    def fail(request):
        raise PadsError('edit_outcome_unknown', 'timeout')
    service.runner = fail
    expect('edit_outcome_unknown', lambda: service.run(preview['preview_id']))
    expect('preview_not_ready', lambda: service.run(preview['preview_id']))
    expect('session_invalidated', lambda: service.sessions.load(sid))


def test_partial_native_failure_keeps_truthful_outcome_and_blocks_replay(context):
    service, sid, _, _, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    service.runner = lambda request: {'ok': False, 'error': {'kind': 'native_edit_failed', 'message': 'partial'}, 'project_write_attempted': True}
    result = service.run(preview['preview_id'])
    assert result['project_write_attempted'] is True
    expect('session_invalidated', lambda: service.sessions.load(sid))
    expect('preview_not_ready', lambda: service.run(preview['preview_id']))


def test_incorrect_success_response_is_not_accepted(context):
    service, sid, _, _, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    service.runner = lambda request: {'ok': True, 'component': BEFORE}
    expect('edit_outcome_unknown', lambda: service.run(preview['preview_id']))
    expect('session_invalidated', lambda: service.sessions.load(sid))


def test_policy_change_disables_preexisting_preview(context):
    service, sid, _, requests, _ = context
    preview = service.preview(sid, TARGET, CHANGE)
    policy = service.state / 'fixture-edits.json'
    policy.write_text(policy.read_text() + '\n')
    expect('write_scope_changed', lambda: service.run(preview['preview_id']))
    assert len(requests) == 1


def test_unconfigured_and_wrong_project_cannot_be_edited(context):
    service, sid, _, requests, fixture = context
    policy = json.loads((service.state / 'fixture-edits.json').read_text())
    expect('write_scope_denied', lambda: check_fixture(policy, fixture / 'source-manifest.json'))
    (service.state / 'fixture-edits.json').unlink()
    expect('write_scope_denied', lambda: service.preview(sid, TARGET, CHANGE))
    assert requests == []


def test_fixture_hard_link_refused(context):
    service, sid, _, requests, fixture = context
    (fixture / 'linked.prj').hardlink_to(fixture / 'LessonFinal/LessonFinal.prj')
    expect('write_scope_denied', lambda: service.preview(sid, TARGET, CHANGE))
    assert requests == []


def test_simultaneous_edit_is_refused(context):
    service, sid, _, requests, _ = context
    with service.lock():
        expect('edit_busy', lambda: service.preview(sid, TARGET, CHANGE))
        expect('edit_busy', lambda: service.sessions.release(sid))
    assert requests == []
