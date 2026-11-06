"""Start an owned acceptance host. No project is opened through public MCP."""
import argparse
import json
import os
import subprocess
from pathlib import Path
from runtime.probe import native_environment, existing_editor
from runtime.diagnostics import installation

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--domain', choices=['designer','layout'], required=True)
    p.add_argument('--sdd-home', type=Path, required=True)
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    a=p.parse_args()
    if installation(a.sdd_home)['com_version'] != '66':
        raise RuntimeError('Acceptance host is validated for COM version 66 only')
    if existing_editor(a.domain):
        raise RuntimeError('editor_already_running')
    state=a.state.resolve()
    state.mkdir(parents=True,exist_ok=True)
    control=state / (a.domain + '.control')
    if control.exists():
        raise RuntimeError('stale fixture control file; inspect before reuse')
    command=[str(Path(os.environ['WINDIR'])/'SysWOW64/WindowsPowerShell/v1.0/powershell.exe'),
        '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(Path(__file__).with_name('fixture_host.ps1')),
        '-Domain',a.domain,'-Fixture',str(a.fixture.resolve()),'-Control',str(control)]
    with (state / (a.domain+'.log')).open('wb') as log:
        host=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            cwd=state,env=native_environment(a.sdd_home.resolve(),state),creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({'host_pid':host.pid,'control':str(control),'log':str(state/(a.domain+'.log'))}))
