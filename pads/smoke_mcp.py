"""Exercise the real stdio executable and installed references, without activation."""
import argparse
import json
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--server', type=Path, required=True)
p.add_argument('--python', type=Path, required=True)
p.add_argument('--sdd-home', type=Path, required=True)
p.add_argument('--state', type=Path, required=True)
a = p.parse_args()
root = Path(__file__).resolve().parent
requests = [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'pads-smoke', 'version': '1'}}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
]
calls = [
    ('get_pads_environment', {}),
    ('get_pads_capabilities', {}),
    ('load_toolset', {'name': 'reference'}),
    ('search_pads_reference', {'query': 'PutTrace', 'limit': 3}),
    ('get_pads_reference_page', {'manual': 'designer', 'page': 1}),
    ('list_pads_interface_members', {'domain': 'layout', 'query': 'PutTrace'}),
    ('get_pads_reference_page', {'manual': 'layout', 'page': True}),
    ('nonexistent_tool', {}),
]
for number, (name, arguments) in enumerate(calls, 3):
    requests.append({'jsonrpc': '2.0', 'id': number, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
requests.append({'jsonrpc': '2.0', 'id': 11, 'method': 'tools/list'})
result = subprocess.run([str(a.server.resolve()), str(a.python.resolve()), str(root / 'worker.py'), str(a.sdd_home.resolve()), str(a.state.resolve())],
                        input='\n'.join(map(json.dumps, requests)) + '\n', text=True, encoding='utf-8',
                        capture_output=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
assert result.returncode == 0, result.stderr
messages = [json.loads(line) for line in result.stdout.splitlines()]
responses = {r['id']: r for r in messages if 'id' in r}
assert len(responses) == 11
assert responses[1]['result']['serverInfo']['name'] == 'pads-automation'
assert len(responses[2]['result']['tools']) == 4
assert len(responses[11]['result']['tools']) == 7
assert sum(r.get('method') == 'notifications/tools/list_changed' for r in messages) == 1

def data(number):
    response = responses[number]['result']
    assert not response.get('isError'), response
    return json.loads(response['content'][0]['text'])

assert data(3)['com_version'] == '66'
assert all(not c['write_verified'] for c in data(4)['capabilities'])
assert data(5)['loaded'] == 'reference'
assert data(6)['matches']
assert data(7)['source']['page_count'] == 776
assert data(8)['matches'] and data(8)['status'] == 'type_information_only'
for number, kind in [(9, 'invalid_argument'), (10, 'unknown_tool')]:
    assert responses[number]['result']['isError']
    assert kind in responses[number]['result']['content'][0]['text']
print(json.dumps({'status': 'passed', 'responses': len(responses), 'domain_tools': 5,
                  'manual_search_hits': len(data(6)['matches']), 'layout_type_member_hits': data(8)['total'],
                  'activation_attempted': False, 'project_write_attempted': False}))
