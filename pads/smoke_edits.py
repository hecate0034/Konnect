"""Explicit write acceptance for the copied official sample, never a production project."""
import argparse
import json
from pathlib import Path
import time

from smoke_sessions import Client
from runtime.diagnostics import fingerprint


def run(server, python, sdd, state, fixture):
    project = (fixture / 'LessonFinal/LessonFinal.prj').resolve(strict=True)
    policy = json.loads((state / 'fixture-edits.json').read_text())
    assert Path(policy['project']).resolve() == project
    target = {'design': 'CORPORATE', 'sheet': 'control_buffers', 'uid': '$5I134', 'path': '$5I134', 'refdes': 'R24'}
    output = state / 'evidence/p2-acceptance-designer.json'
    report = {'status': 'running', 'target': target, 'project': str(project), 'steps': []}
    def record(step, data):
        report['steps'].append({'step': step, 'data': data})
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(step, flush=True)
        return data
    client = Client([str(server), str(python), str(Path(__file__).with_name('worker.py')), str(sdd), str(state)], timeout=170)
    start = time.monotonic()
    try:
        client.request('initialize')
        client.call('load_toolset', {'name': 'session'})
        client.call('load_toolset', {'name': 'edit'})
        binding = record('bind', client.call('bind_pads_session', {'domain': 'designer', 'expected_project': str(project)}))
        args = {'session_id': binding['session_id'], 'target': target}
        original = record('inspect_before', client.call('inspect_pads_component', args))['component']
        changes = {'attribute': {'name': 'Description', 'value': 'P2 test copy - R24 x=' + str(original['position']['x'] + 10)},
                   'position': {**original['position'], 'x': original['position']['x'] + 10}}
        record('unsupported_identity_edit_refused', client.call('preview_pads_component_edit',
            {**args, 'changes': {'attribute': {'name': 'Ref Designator', 'value': 'R999'}}}, 'invalid_argument'))
        preview = record('preview', client.call('preview_pads_component_edit', {**args, 'changes': changes}))
        stale = record('second_preview', client.call('preview_pads_component_edit', {**args, 'changes': changes}))
        unchanged = record('preview_did_not_write', client.call('inspect_pads_component', args))
        assert unchanged['component'] == original
        executed = record('execute', client.call('execute_pads_component_edit', {'preview_id': preview['preview_id']}))
        assert executed['component'] == preview['after']
        record('replay_refused', client.call('execute_pads_component_edit', {'preview_id': preview['preview_id']}, 'preview_not_ready'))
        stale_result = record('stale_preview_refused', client.call('execute_pads_component_edit', {'preview_id': stale['preview_id']}, 'stale_preview'))
        assert stale_result['project_write_attempted'] is False
        readback = record('independent_readback', client.call('inspect_pads_component', args))
        assert readback['component'] == preview['after']
        assert readback['component']['connections'] == original['connections']
        persisted = record('close_reopen', client.call('reopen_pads_edit', {'preview_id': preview['preview_id']}))
        assert persisted['status'] == 'persistence_verified'
        record('old_binding_refused', client.call('get_pads_session', {'session_id': binding['session_id']}, 'session_invalidated'))
        rebound = record('rebind', client.call('bind_pads_session', {'domain': 'designer', 'expected_project': str(project)}))
        final = record('read_after_reopen', client.call('inspect_pads_component', {'session_id': rebound['session_id'], 'target': target}))
        assert final['component'] == preview['after']
        record('release', client.call('release_pads_session', {'session_id': rebound['session_id']}))
        manifest = json.loads((fixture / 'source-manifest.json').read_text())
        source = Path(manifest['source'])
        assert all(fingerprint(source / rel) == sha for rel, sha in manifest['files'].items())
        report.update(status='passed', requests=client.number, seconds=round(time.monotonic() - start, 2),
                      source_files_unchanged=len(manifest['files']), persistence='native_database_close_reopen_verified')
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        return {key: value for key, value in report.items() if key != 'steps'}
    except Exception as exc:
        report.update(status='failed', failure=str(exc), requests=client.number)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        raise
    finally:
        client.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ('server', 'python', 'sdd', 'state', 'fixture'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(run(*(getattr(a, name).resolve() for name in ('server', 'python', 'sdd', 'state', 'fixture')))))
