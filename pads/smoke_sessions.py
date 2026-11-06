"""Real MCP acceptance against an already opened, prepared evaluation copy."""
import argparse
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from runtime.diagnostics import fingerprint


class Client:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        def receive():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()
        self.number = 0

    def request(self, method, params=None):
        self.number += 1
        self.process.stdin.write(json.dumps({'jsonrpc':'2.0','id':self.number,'method':method,'params':params or {}})+'\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 100
        while True:
            response = self.messages.get(timeout=max(0.1, deadline-time.monotonic()))
            if response.get('id') == self.number:
                return response['result']

    def call(self, name, args, error=None):
        result = self.request('tools/call', {'name': name, 'arguments': args})
        data = json.loads(result['content'][0]['text'])
        if error:
            assert result.get('isError') and data['error']['kind'] == error, data
        else:
            assert not result.get('isError'), data
        return data

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()


def run(server, python, sdd, state, fixture, domain, project_override=None):
    root = Path(__file__).resolve().parent
    relative = {'designer':'LessonFinal/LessonFinal.prj','layout':'LessonFinal/PCB/CORPORATE.pcb'}[domain]
    project = (project_override or fixture / relative).resolve(strict=True)
    assert project.is_relative_to(fixture.resolve()), 'project must be inside the prepared fixture'
    assert (fixture/'source-manifest.json').is_file(), 'prepared fixture required'
    baseline = fingerprint(project)
    wrong = state / ('wrong-target' + project.suffix)
    if not wrong.exists():
        wrong.write_bytes(project.read_bytes())
    client = Client([str(server), str(python), str(root/'worker.py'), str(sdd), str(state)])
    start = time.monotonic()
    try:
        client.request('initialize', {'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'p1-acceptance','version':'1'}})
        client.call('load_toolset', {'name':'session'})
        client.call('bind_pads_session', {'domain':domain,'expected_project':str(wrong)}, 'target_mismatch')
        binding = client.call('bind_pads_session', {'domain':domain,'expected_project':str(project)})
        session = {'session_id':binding['session_id']}
        live = client.call('get_pads_session', session)
        assert live['identity'] == binding['identity']
        snapshot = client.call('read_pads_project', session)
        assert snapshot['counts']['components'] > 0 and snapshot['counts']['nets'] > 0
        assert all(item['uid'] for kind in ('components','nets') for item in snapshot['inventory'][kind])
        matched = client.call('validate_pads_snapshot', {**session, 'snapshot_token':snapshot['snapshot_token']})
        assert matched['matches'] and not matched['write_authorized']
        released = client.call('release_pads_session', session)
        assert not released['application_closed']
        client.call('get_pads_session', session, 'session_invalidated')
        rebound = client.call('bind_pads_session', {'domain':domain,'expected_project':str(project)})
        assert rebound['identity'] == binding['identity'] and rebound['session_id'] != binding['session_id']
        client.call('release_pads_session', {'session_id':rebound['session_id']})
        assert fingerprint(project) == baseline
        report = {'status':'passed','domain':domain,'requests':client.number,'seconds':round(time.monotonic()-start,2),
                  'counts':snapshot['counts'],'identity':binding['identity'],'project_sha256':baseline,
                  'checks':['wrong_target_refused','live_binding','nonempty_native_ids','snapshot_readback',
                            'release_keeps_editor','released_session_refused','explicit_rebind','project_file_unchanged'],
                  'project_write_attempted':False}
        (state/'evidence'/('p1-acceptance-'+domain+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
        (state/'evidence'/('p1-snapshot-'+domain+'.json')).write_text(json.dumps(snapshot,indent=2),encoding='utf-8')
        return report
    finally:
        client.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    for name in ('server','python','sdd','state','fixture'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--domain',choices=['designer','layout'],required=True)
    p.add_argument('--project',type=Path)
    a=p.parse_args()
    print(json.dumps(run(a.server.resolve(),a.python.resolve(),a.sdd.resolve(),a.state.resolve(),a.fixture.resolve(),a.domain,a.project)))
