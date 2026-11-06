"""Opt-in for one disposable prepared fixture. Never exposed as an MCP tool."""
import argparse
import json
from pathlib import Path
from runtime.diagnostics import fingerprint
from runtime.edits import check_fixture


def configure(state, fixture):
    state, fixture = state.resolve(strict=True), fixture.resolve(strict=True)
    if not fixture.is_relative_to(state):
        raise ValueError('The disposable fixture must be inside the local runtime state directory')
    manifest = fixture / 'source-manifest.json'
    source = Path(json.loads(manifest.read_text(encoding='utf-8'))['source']).resolve(strict=True)
    policy = {'fixture': str(fixture), 'source': str(source),
              'project': str(fixture / 'LessonFinal/LessonFinal.prj'), 'manifest_sha256': fingerprint(manifest)}
    check_fixture(policy, policy['project'])
    policy_path = state / 'fixture-edits.json'
    if policy_path.exists():
        raise ValueError('Fixture edit policy already exists; inspect it before replacing it')
    policy_path.write_text(json.dumps(policy, indent=2), encoding='utf-8')
    return policy


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(configure(args.state, args.fixture)))
