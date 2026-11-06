import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.sessions import PadsError, Sessions, snapshot_digest


@pytest.fixture
def context(tmp_path):
    sdd = tmp_path / 'installed'
    sdd.mkdir()
    executable = sdd / 'viewdraw.exe'
    executable.write_bytes(b'test executable')
    project = tmp_path / 'fixture.prj'
    project.write_bytes(b'test project')
    identity = {'domain': 'designer', 'project_path': str(project), 'instance_guid': 'instance-A',
                'pid': 42, 'process_started': '1000', 'executable': str(executable)}
    inventory = {'pages': [{'sheet': '1'}], 'components': [{'uid': 'C1', 'refdes': 'R1'}], 'nets': [{'uid': 'N1', 'name': 'GND'}]}
    calls = []
    response = {'ok': True, 'identity': identity, 'inventory': inventory}
    def reader(request):
        calls.append(request)
        return copy.deepcopy(response)
    service = Sessions(sdd, tmp_path, reader)
    return service, project, response, calls


def assert_kind(kind, action):
    with pytest.raises(PadsError) as error:
        action()
    assert error.value.kind == kind


def test_explicit_target_and_successful_live_read(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    snapshot = service.read(record['session_id'], inventory=True)
    assert snapshot['counts'] == {'pages': 1, 'components': 1, 'nets': 1}
    assert snapshot['inventory'] == response['inventory']
    assert calls[-1]['expected_identity'] == record['identity']
    assert service.validate(record['session_id'], snapshot['snapshot_token'])['matches']


def test_wrong_project_refused_before_binding(context):
    service, project, response, calls = context
    other = project.with_name('other.prj')
    other.write_bytes(b'other')
    assert_kind('target_mismatch', lambda: service.bind('designer', str(other)))
    with service.db() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0


@pytest.mark.parametrize('field,value', [('pid', 43), ('process_started', '2000'), ('instance_guid', 'instance-B')])
def test_restarted_or_reused_process_invalidates_old_session(context, field, value):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    response['identity'][field] = value
    assert_kind('session_disconnected', lambda: service.read(record['session_id']))
    assert_kind('session_invalidated', lambda: service.read(record['session_id']))
    assert service.bind('designer', str(project))['session_id'] != record['session_id']


def test_file_changed_requires_rebind_before_native_call(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    project.write_bytes(b'changed')
    assert_kind('project_file_changed', lambda: service.read(record['session_id']))
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['application_busy_or_modal', 'native_timeout', 'application_not_running', 'target_mismatch'])
def test_native_failures_cannot_reuse_a_stale_session(context, kind):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    def fail(request):
        raise PadsError(kind, 'injected failure')
    service.reader = fail
    assert_kind(kind, lambda: service.read(record['session_id']))
    assert_kind('session_invalidated', lambda: service.read(record['session_id']))


def test_changed_inventory_rejects_previous_snapshot(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    snapshot = service.read(record['session_id'], inventory=True)
    response['inventory']['components'][0]['refdes'] = 'R2'
    assert_kind('stale_snapshot', lambda: service.validate(record['session_id'], snapshot['snapshot_token']))


def test_release_never_calls_or_closes_the_editor(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    count = len(calls)
    assert not service.release(record['session_id'])['application_closed']
    assert len(calls) == count
    assert_kind('session_invalidated', lambda: service.read(record['session_id']))


def test_foreign_installation_rejected(context, tmp_path):
    service, project, response, calls = context
    foreign = tmp_path / 'foreign.exe'
    foreign.write_bytes(b'other installation')
    response['identity']['executable'] = str(foreign)
    assert_kind('identity_unverified', lambda: service.bind('designer', str(project)))


def test_bad_target_and_session_id_never_call_native(context):
    service, project, response, calls = context
    for path in ('relative.prj', str(project.parent)):
        assert_kind('invalid_target', lambda: service.bind('designer', path))
    assert_kind('invalid_session_id', lambda: service.read('../session'))
    assert not calls


def test_digest_ignores_enumeration_order_but_keeps_duplicates():
    a = {'pages': [], 'components': [], 'nets': [{'uid': '2'}, {'uid': '1'}]}
    b = {**a, 'nets': list(reversed(a['nets']))}
    assert snapshot_digest(a)[0] == snapshot_digest(b)[0]
    b['nets'].append({'uid': '1'})
    assert snapshot_digest(a)[0] != snapshot_digest(b)[0]


def test_project_switch_during_native_read_is_rejected(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    other = project.with_name('other.prj')
    other.write_bytes(b'other')
    response['identity']['project_path'] = str(other)
    assert_kind('target_mismatch', lambda: service.read(record['session_id'], inventory=True))


def test_missing_native_ids_cannot_be_reported_as_success(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    response['inventory']['nets'][0]['uid'] = ''
    assert_kind('missing_object_identity', lambda: service.read(record['session_id'], inventory=True))
    assert_kind('session_invalidated', lambda: service.read(record['session_id']))


def test_concurrent_release_wins_over_late_native_result(context):
    service, project, response, calls = context
    record = service.bind('designer', str(project))
    def release_during_read(request):
        service.release(record['session_id'])
        return copy.deepcopy(response)
    service.reader = release_during_read
    assert_kind('session_invalidated', lambda: service.read(record['session_id'], inventory=True))
