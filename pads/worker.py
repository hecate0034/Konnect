"""One read-only request per process; no arbitrary command or native activation."""
from __future__ import annotations

import json
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
site.addsitedir(str(ROOT / '.pads-runtime/python'))
from runtime.diagnostics import installation, registered_servers, fingerprint
from runtime.reference import search, get_page
from runtime.sessions import Sessions, PadsError


def dispatch(name, args, sdd, state):
    if name in ('bind_pads_session', 'get_pads_session', 'read_pads_project', 'validate_pads_snapshot', 'release_pads_session'):
        sessions = Sessions(sdd, state)
        if name == 'bind_pads_session':
            return sessions.bind(args['domain'], args['expected_project'])
        if name == 'get_pads_session':
            return sessions.read(args['session_id'])
        if name == 'read_pads_project':
            return sessions.read(args['session_id'], inventory=True)
        if name == 'validate_pads_snapshot':
            return sessions.validate(args['session_id'], args['snapshot_token'])
        return sessions.release(args['session_id'])
    if name == 'get_pads_environment':
        result = installation(sdd)
        result.update(registrations=registered_servers(result['com_version']), status='static_evidence_only')
        return result
    if name == 'get_pads_capabilities':
        capabilities = []
        for domain in ['designer', 'layout', 'cell', 'parts', 'padstack']:
            path = state / 'evidence' / f'activation-{domain}.json'
            evidence = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
            acceptance = state / 'evidence' / f'p1-acceptance-{domain}.json'
            read_evidence = json.loads(acceptance.read_text(encoding='utf-8')) if acceptance.is_file() else None
            capabilities.append({'domain': domain, 'activation_evidence': evidence,
                                 'read_project_implementation_available': domain in ('designer', 'layout'),
                                 'recorded_read_acceptance': read_evidence,
                                 'read_project_verified': bool(read_evidence and read_evidence.get('status') == 'passed'),
                                 'write_verified': False})
        return {'capabilities': capabilities, 'source_kind': 'recorded_probe_not_live_state',
                'note': 'All acceptance evidence is historical and specific to the recorded fixture. It does not prove a live connection, general compatibility or writes.'}
    if name == 'search_pads_reference':
        return search(state / 'reference.sqlite', args['query'], args.get('limit', 8))
    if name == 'get_pads_reference_page':
        return get_page(state / 'reference.sqlite', args['manual'], args['page'])
    if name == 'list_pads_interface_members':
        data = json.loads((state / 'evidence/typelibs.json').read_text(encoding='utf-8'))
        domain = data[args['domain']]
        if fingerprint(Path(domain['path'])) != domain['sha256']:
            raise ValueError('type_library_changed')
        query = args['query'].casefold()
        matches = []
        for interface in domain['interfaces']:
            for member in interface['members']:
                if query in member['name'].casefold() or query in interface['name'].casefold():
                    matches.append({'interface': interface['name'], **member})
        return {'matches': matches[:100], 'total': len(matches), 'truncated': len(matches) > 100,
                'source': domain['path'], 'sha256': domain['sha256'], 'version': domain['version'],
                'status': 'type_information_only'}
    raise ValueError('unknown_tool')


def main():
    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        result = dispatch(request['name'], request['arguments'], Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False))
    except PadsError as exc:
        print(json.dumps({'ok': False, 'error': {'kind': exc.kind, 'message': str(exc)}, 'project_write_attempted': False}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': {'kind': 'read_failed', 'message': str(exc)}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
