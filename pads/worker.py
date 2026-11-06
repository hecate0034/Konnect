"""One bounded request per process; no arbitrary command or native activation."""
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
from runtime.edits import Edits
from runtime.schematic import Schematic


def dispatch(name, args, sdd, state):
    if name in ('inspect_pads_sheet','preview_pads_schematic_change','execute_pads_schematic_change',
                'preview_pads_schematic_recovery','reopen_pads_schematic_change','check_pads_schematic'):
        service=Schematic(sdd,state)
        if name=='check_pads_schematic':return service.check_sheet(args['session_id'],args['design'],args['sheet'],args.get('baseline_id'))
        if name=='inspect_pads_sheet':return service.inspect_sheet(args['session_id'],args['design'],args['sheet'])
        if name=='preview_pads_schematic_change':return service.preview_change(args['session_id'],args['design'],args['sheet'],args['operation'])
        if name=='execute_pads_schematic_change':return service.execute_change(args['preview_id'])
        if name=='preview_pads_schematic_recovery':return service.preview_recovery(args['session_id'],args['change_id'])
        return service.reopen_change(args['session_id'],args['change_id'])
    if name in ('inspect_pads_component', 'preview_pads_component_edit', 'execute_pads_component_edit', 'reopen_pads_edit'):
        edits = Edits(sdd, state)
        if name == 'inspect_pads_component':
            return edits.inspect(args['session_id'], args['target'])
        if name == 'preview_pads_component_edit':
            return edits.preview(args['session_id'], args['target'], args['changes'])
        return edits.run(args['preview_id'], reopen=name == 'reopen_pads_edit')
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
            edit_path = state / 'evidence' / f'p2-acceptance-{domain}.json'
            edit_evidence = json.loads(edit_path.read_text(encoding='utf-8')) if edit_path.is_file() else None
            edit_passed = bool(edit_evidence and edit_evidence.get('status') == 'passed')
            sheet_path=state/'evidence'/f'p2b-acceptance-{domain}.json'
            sheet_evidence=json.loads(sheet_path.read_text(encoding='utf-8')) if sheet_path.is_file() else None
            route_path=state/'evidence'/f'p2c-acceptance-{domain}.json'
            route_evidence=json.loads(route_path.read_text(encoding='utf-8')) if route_path.is_file() else None
            capabilities.append({'domain': domain, 'activation_evidence': evidence,
                                 'read_project_implementation_available': domain in ('designer', 'layout'),
                                 'recorded_read_acceptance': read_evidence,
                                 'read_project_verified': bool(read_evidence and read_evidence.get('status') == 'passed'),
                                 'write_verified': False,
                                 'bounded_test_edit_verified': edit_passed,
                                 'bounded_sheet_change_verified':bool(sheet_evidence and sheet_evidence.get('status')=='passed'),
                                 'recorded_sheet_acceptance':sheet_evidence,
                                 'bounded_routing_verified':bool(route_evidence and route_evidence.get('status')=='passed'),
                                 'recorded_route_acceptance':route_evidence,
                                 'verified_edit_scope': {'component': edit_evidence.get('target'),
                                     'attribute': 'Description instance value', 'position_unit': '0.01_inch',
                                     'persistence': edit_evidence.get('persistence'), 'project': edit_evidence.get('project')}
                                     if edit_passed else None})
        return {'capabilities': capabilities, 'source_kind': 'recorded_probe_not_live_state',
                'note': 'Historical fixture-specific evidence, not live state. General writes remain unverified; bounded_test_edit_verified covers only the listed test operation.'}
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
    request = {}
    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        result = dispatch(request['name'], request['arguments'], Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(result if result.get('ok') is False else {'ok': True, 'data': result}, ensure_ascii=False))
    except PadsError as exc:
        possible_write = request.get('name') in ('execute_pads_component_edit', 'reopen_pads_edit',
                                               'execute_pads_schematic_change','reopen_pads_schematic_change')
        print(json.dumps({'ok': False, 'error': {'kind': exc.kind, 'message': str(exc)},
                          'project_write_attempted': None if possible_write else False}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': {'kind': 'worker_failed', 'message': str(exc)},
                          'project_write_attempted': None}, ensure_ascii=False))


if __name__ == '__main__':
    main()
