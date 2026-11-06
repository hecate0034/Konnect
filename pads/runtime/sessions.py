"""Explicit, fail-closed bindings to running native applications and project files."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .diagnostics import fingerprint, installation
from .probe import native_environment


class PadsError(Exception):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


def project_path(value, domain):
    path = Path(value)
    if domain not in ('designer', 'layout') or not path.is_absolute():
        raise PadsError('invalid_target', 'An explicit absolute project path and known domain are required')
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise PadsError('target_unavailable', 'The specified project file is unavailable') from exc
    if not path.is_file() or path.suffix.lower() != {'designer': '.prj', 'layout': '.pcb'}[domain]:
        raise PadsError('invalid_target', 'Expected a Designer .prj or Layout .pcb file')
    return path


def native_read(sdd: Path, state: Path, request):
    driver = {'designer': 'native_designer.py', 'layout': 'native_layout.py'}.get(request['domain'])
    if driver is None:
        raise PadsError('invalid_argument', 'Unsupported native read domain')
    command = [sys.executable, '-X', 'utf8', str(Path(__file__).with_name(driver))]
    request = {**request, 'com_version': installation(sdd)['com_version']}
    try:
        completed = subprocess.run(command, input=json.dumps(request).encode('utf-8'),
                                   capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW,
                                   env=native_environment(sdd, state))
    except subprocess.TimeoutExpired as exc:
        stages = (exc.stderr or b'').decode('utf-8', errors='replace').strip().splitlines()
        last = stages[-1] if stages else 'native_read'
        raise PadsError('native_timeout', 'Read worker stopped at ' + last + '; editor was not terminated. Rebind after checking its state.') from exc
    if completed.returncode:
        raise PadsError('native_worker_failed', 'Native read process failed')
    try:
        response = json.loads(completed.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise PadsError('invalid_native_response', 'Native read did not return a valid response') from exc
    if response.get('ok') is not True:
        error = response.get('error', {})
        raise PadsError(error.get('kind', 'native_read_failed'), error.get('message', 'Native read failed'))
    return response


def snapshot_digest(inventory):
    if not isinstance(inventory, dict) or set(inventory) != {'pages', 'components', 'nets'}:
        raise PadsError('invalid_inventory', 'Expected pages, components and nets')
    for key, rows in inventory.items():
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise PadsError('invalid_inventory', 'Invalid native collection')
        if key != 'pages' and any(not isinstance(row.get('uid'), str) or not row['uid'] for row in rows):
            raise PadsError('missing_object_identity', 'Native object identity is missing')
    # Enumeration order is not identity. Preserve all objects, including hierarchy instances.
    normalized = {key: sorted(value, key=lambda item: json.dumps(item, sort_keys=True))
                  for key, value in inventory.items()}
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest(), normalized


class Sessions:
    def __init__(self, sdd: Path, state: Path, reader=None):
        self.sdd = sdd.resolve(strict=True)
        self.state = state.resolve(strict=True)
        self.reader = reader or (lambda request: native_read(self.sdd, self.state, request))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.state / 'sessions.sqlite', timeout=5)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, record TEXT NOT NULL, reason TEXT)')
            with db:
                yield db
        finally:
            db.close()

    def load(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch('[a-f0-9]{32}', session_id):
            raise PadsError('invalid_session_id', 'Invalid session identifier')
        with self.db() as db:
            row = db.execute('SELECT state,record,reason FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise PadsError('session_not_found', 'Bind a session first')
        if row[0] != 'active':
            raise PadsError('session_invalidated', 'Session is ' + row[0] + ': ' + str(row[2]))
        return json.loads(row[1])

    def invalidate(self, session_id, reason):
        with self.db() as db:
            db.execute("UPDATE sessions SET state='invalidated',reason=? WHERE id=? AND state='active'", (reason, session_id))

    def check_identity(self, identity, domain, path, expected=None):
        if identity.get('domain') != domain or project_path(identity.get('project_path', ''), domain) != path:
            raise PadsError('target_mismatch', 'Native application reports a different target')
        try:
            executable = Path(identity['executable']).resolve(strict=True)
            if not executable.is_relative_to(self.sdd):
                raise ValueError('foreign installation')
            if not identity['instance_guid'] or int(identity['pid']) <= 0 or not identity['process_started']:
                raise ValueError('incomplete identity')
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise PadsError('identity_unverified', 'Application identity or installation could not be verified') from exc
        if expected and any(identity.get(k) != expected.get(k) for k in ('instance_guid', 'pid', 'process_started', 'executable')):
            raise PadsError('session_disconnected', 'The bound application instance changed')

    def bind(self, domain, expected_project):
        path = project_path(expected_project, domain)
        sha = fingerprint(path)
        result = self.reader({'domain': domain, 'expected_path': str(path), 'inventory': False})
        identity = result['identity']
        self.check_identity(identity, domain, path)
        if fingerprint(path) != sha:
            raise PadsError('target_changed_during_read', 'Project file changed during binding')
        session_id = uuid.uuid4().hex
        record = {'session_id': session_id, 'domain': domain, 'identity': identity, 'project_sha256': sha,
                  'bound_at': datetime.now(timezone.utc).isoformat(), 'project_write_attempted': False,
                  'mode': 'attach_existing_read_only'}
        with self.db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,NULL)', (session_id, 'active', json.dumps(record)))
        return record

    def read(self, session_id, inventory=False):
        record = self.load(session_id)
        try:
            identity = record['identity']
            path = project_path(identity['project_path'], record['domain'])
            if fingerprint(path) != record['project_sha256']:
                raise PadsError('project_file_changed', 'Project file changed since binding; bind again')
            result = self.reader({'domain': record['domain'], 'expected_path': str(path),
                                  'expected_identity': identity, 'inventory': inventory})
            self.check_identity(result['identity'], record['domain'], path, identity)
            if fingerprint(path) != record['project_sha256']:
                raise PadsError('target_changed_during_read', 'Project file changed while reading')
            self.load(session_id)  # Concurrent release/invalidation must win over a late read.
            response = {**record, 'state': 'active', 'live_validated': True}
            if inventory:
                digest, data = snapshot_digest(result['inventory'])
                response.update(inventory=data, snapshot_token=hashlib.sha256(
                    (session_id + record['project_sha256'] + digest).encode('ascii')).hexdigest(),
                    counts={key: len(value) for key, value in data.items()},
                    snapshot_scope='listed_object_identity_and_attributes_only')
            return response
        except PadsError as exc:
            self.invalidate(session_id, exc.kind)
            raise
        except Exception as exc:
            self.invalidate(session_id, 'unverified_read')
            raise PadsError('unverified_read', 'Read could not be verified; bind again') from exc

    def validate(self, session_id, snapshot_token):
        current = self.read(session_id, inventory=True)
        if current['snapshot_token'] != snapshot_token:
            raise PadsError('stale_snapshot', 'Listed native object state changed; read a fresh snapshot')
        return {'session_id': session_id, 'snapshot_token': snapshot_token, 'matches': True,
                'project_write_attempted': False, 'write_authorized': False,
                'scope': current['snapshot_scope'], 'note': 'A read-time comparison, not a write transaction or geometry/netlist guarantee.'}

    def release(self, session_id):
        self.load(session_id)
        with self.db() as db:
            db.execute("UPDATE sessions SET state='released',reason='explicit_release' WHERE id=? AND state='active'", (session_id,))
        return {'session_id': session_id, 'state': 'released', 'application_closed': False, 'project_write_attempted': False}
