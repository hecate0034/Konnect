"""P2: bounded, single-use edits of existing components in configured test copies."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from .diagnostics import fingerprint, installation
from .probe import native_environment
from .sessions import PadsError, Sessions
from .locking import edit_lock

ATTRIBUTES = ('Description', 'Value', 'Tolerance', 'Power', 'Cost')
TARGET_KEYS = {'design', 'sheet', 'uid', 'path', 'refdes'}


def validate_target(target):
    if not isinstance(target, dict) or set(target) != TARGET_KEYS or any(
            not isinstance(v, str) or not v or len(v) > 256 for v in target.values()):
        raise PadsError('invalid_target', 'Require design, sheet, UID, path and nonempty reference designator')
    return target


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or set(changes) - {'attribute', 'position'}:
        raise PadsError('invalid_change', 'Specify an existing attribute and/or an absolute position')
    if 'attribute' in changes:
        attr = changes['attribute']
        if (not isinstance(attr, dict) or set(attr) != {'name', 'value'} or attr['name'] not in ATTRIBUTES
                or not isinstance(attr['value'], str) or not attr['value'] or len(attr['value']) > 1024
                or any(ord(c) < 32 for c in attr['value'])):
            raise PadsError('invalid_change', 'Unsupported attribute or invalid nonempty instance value')
    if 'position' in changes:
        pos = changes['position']
        if (not isinstance(pos, dict) or set(pos) != {'x', 'y', 'unit'} or pos['unit'] != '0.01_inch'
                or any(type(pos[k]) is not int or not 0 <= pos[k] <= 10000 for k in ('x', 'y'))):
            raise PadsError('invalid_change', 'Position requires integer x/y in 0.01_inch, range 0..10000')
    return changes


def check_fixture(policy, path):
    """Local deployment guard, not a security boundary against policy-file editors."""
    root = Path(policy['fixture']).resolve(strict=True)
    project = Path(policy['project']).resolve(strict=True)
    source = Path(policy['source']).resolve(strict=True)
    if (project != Path(path).resolve(strict=True) or project != root / 'LessonFinal/LessonFinal.prj'
            or root.is_relative_to(source) or source.is_relative_to(root)):
        raise PadsError('write_scope_denied', 'Only the explicitly configured disposable test project may be edited')
    # Resolve/reparse checks cover directories and database files, including hard links.
    for p in [root, *root.rglob('*')]:
        if p.is_symlink() or p.is_junction() or not p.resolve().is_relative_to(root):
            raise PadsError('write_scope_denied', 'Linked fixture paths are not supported')
        if p.is_file() and p.stat().st_nlink != 1:
            raise PadsError('write_scope_denied', 'Hard-linked fixture files are not supported')
    if fingerprint(root / 'source-manifest.json') != policy['manifest_sha256']:
        raise PadsError('write_scope_denied', 'Fixture source manifest changed')
    return root


def native_edit(sdd, state, request, driver='native_edit.py'):
    request = {**request, 'com_version': installation(sdd)['com_version']}
    try:
        result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).with_name(driver))],
                                input=json.dumps(request).encode('utf-8'), capture_output=True,
                                timeout=180 if driver=='native_schematic.py' else 60,
                                env=native_environment(sdd, state), creationflags=subprocess.CREATE_NO_WINDOW)
        response = json.loads(result.stdout.decode('utf-8-sig'))
        if result.returncode or not isinstance(response, dict):
            raise ValueError('invalid response')
        return response
    except (subprocess.TimeoutExpired, ValueError, UnicodeError) as exc:
        raise PadsError('edit_outcome_unknown', 'Native worker did not finish reliably. Do not retry; inspect the test copy.') from exc


class Edits:
    def __init__(self, sdd, state, sessions=None, runner=None, clock=time.time):
        self.sdd, self.state = Path(sdd), Path(state)
        self.sessions = sessions or Sessions(self.sdd, self.state)
        self.runner = runner or (lambda request: native_edit(self.sdd, self.state, request))
        self.clock = clock

    def lock(self):
        # OS lock is released on worker death. A claimed preview remains non-retryable.
        return edit_lock(self.state)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.state / 'edits.sqlite')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS edits (id TEXT PRIMARY KEY, status TEXT NOT NULL, record TEXT NOT NULL)')
            with db:
                yield db
        finally:
            db.close()

    def scope(self, record):
        policy_path = self.state / 'fixture-edits.json'
        if not policy_path.is_file() or record['domain'] != 'designer':
            raise PadsError('write_scope_denied', 'Designer test-copy edits have not been configured')
        policy = json.loads(policy_path.read_text(encoding='utf-8'))
        check_fixture(policy, record['identity']['project_path'])
        return policy, fingerprint(policy_path)

    def request(self, session_id, target, action, **extra):
        validate_target(target)
        record = self.sessions.read(session_id)
        policy, digest = self.scope(record)
        return {'action': action, 'domain': 'designer', 'expected_path': record['identity']['project_path'],
                'expected_identity': record['identity'], 'policy': policy, 'policy_sha256': digest,
                'target': target, **extra}

    @staticmethod
    def checked(response):
        if response.get('ok') is not True:
            error = response.get('error', {})
            raise PadsError(error.get('kind', 'edit_read_failed'), error.get('message', 'Native component read failed'))
        return response

    def inspect(self, session_id, target):
        with self.lock():
            return self.checked(self.runner(self.request(session_id, target, 'inspect')))

    def preview(self, session_id, target, changes):
        validate_changes(changes)
        with self.lock():
            request = self.request(session_id, target, 'inspect')
            state = self.checked(self.runner(request))['component']
            self.sessions.load(session_id)
            # The native worker uses the same policy before touching any setter.
            desired = proposed_state(state, changes)
            if desired == state:
                raise PadsError('no_change', 'Proposed values already match the component')
            preview_id = uuid.uuid4().hex
            record = {'preview_id': preview_id, 'session_id': session_id, 'target': target,
                      'before': state, 'after': desired, 'changes': changes,
                      'policy_sha256': request['policy_sha256'], 'expires_at': self.clock() + 600,
                      'created_at': datetime.now(timezone.utc).isoformat(),
                      'scope': 'single_existing_schematic_component_in_test_copy',
                      'persistence': 'Designer writes the project database immediately; no unsaved-change guarantee',
                      'connection_guard': 'component pin/net identities; not full-sheet geometry or ERC',
                      'project_write_attempted': False}
            with self.db() as db:
                db.execute('INSERT INTO edits VALUES (?,?,?)', (preview_id, 'ready', json.dumps(record)))
            return record

    def load(self, preview_id, required_status):
        if not isinstance(preview_id, str) or not re.fullmatch('[a-f0-9]{32}', preview_id):
            raise PadsError('invalid_preview', 'Invalid preview ID')
        with self.db() as db:
            row = db.execute('SELECT status,record FROM edits WHERE id=?', (preview_id,)).fetchone()
        if row is None or row[0] != required_status:
            raise PadsError('preview_not_ready', 'Preview missing, consumed or in an incompatible state; do not retry')
        return json.loads(row[1])

    def persist(self, record, status):
        with self.db() as db:
            db.execute('UPDATE edits SET status=?,record=? WHERE id=?',
                       (status, json.dumps(record), record['preview_id']))

    def run(self, preview_id, reopen=False):
        with self.lock():
            record = self.load(preview_id, 'executed' if reopen else 'ready')
            if not reopen and self.clock() > record['expires_at']:
                self.persist(record, 'expired')
                raise PadsError('preview_expired', 'Create a fresh preview')
            request = self.request(record['session_id'], record['target'], 'reopen' if reopen else 'execute',
                                   before=record['after'] if reopen else record['before'],
                                   after=record['after'], changes=record['changes'], expires_at=record['expires_at'])
            if request['policy_sha256'] != record['policy_sha256']:
                raise PadsError('write_scope_changed', 'Fixture policy changed; create a fresh preview')
            self.persist(record, 'reopening' if reopen else 'executing')
            try:
                response = self.runner(request)
            except Exception:
                self.persist(record, 'outcome_unknown')
                self.sessions.invalidate(record['session_id'], 'edit_outcome_unknown')
                raise
            record['result'] = response
            if response.get('ok') is not True:
                self.persist(record, 'failed')
                if response.get('project_write_attempted') is not False or reopen:
                    self.sessions.invalidate(record['session_id'], 'edit_needs_inspection')
                return {'preview_id': preview_id, **response}
            if response.get('component') != record['after']:
                self.persist(record, 'readback_mismatch')
                self.sessions.invalidate(record['session_id'], 'edit_readback_mismatch')
                raise PadsError('edit_outcome_unknown', 'Native response differs from approved preview; inspect before further edits')
            if reopen:
                self.sessions.invalidate(record['session_id'], 'project_reopened_bind_again')
                self.persist(record, 'persistence_verified')
            else:
                # P1 sessions remain truthful about writes, and keep their file baseline current.
                current = self.sessions.load(record['session_id'])
                current.update(mode='test_fixture_edited', project_write_attempted=True,
                               project_sha256=fingerprint(Path(current['identity']['project_path'])))
                with self.sessions.db() as db:
                    db.execute("UPDATE sessions SET record=? WHERE id=? AND state='active'",
                               (json.dumps(current), record['session_id']))
                self.persist(record, 'executed')
            return {'preview_id': preview_id, 'status': 'persistence_verified' if reopen else 'executed', **response}


def proposed_state(before, changes):
    validate_changes(changes)
    after = json.loads(json.dumps(before))
    if 'attribute' in changes:
        change = changes['attribute']
        matches = [a for a in after['attributes'] if a['name'] == change['name']]
        if len(matches) != 1:
            raise PadsError('attribute_not_unique', 'The requested existing attribute is missing or ambiguous')
        matches[0]['instance_value'] = change['value']
        matches[0]['effective_value'] = change['value']
    if 'position' in changes:
        if 'wire_geometry' in after:
            from .geometry import move_geometry, wire_issues
            move_geometry(after, changes['position'], after['wire_geometry'])
            if wire_issues([after],after['wire_geometry']):
                raise PadsError('geometry_invalid', 'Proposed move leaves a pin gap or collapsed wire')
        else:
            after['position'] = changes['position'].copy()
    return after
