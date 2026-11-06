import struct
import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.diagnostics import installation, pe_architecture
from runtime.probe import activate
from runtime import probe


def test_installation_requires_numeric_version(tmp_path):
    standard = tmp_path / "standard"
    standard.mkdir()
    xml = standard / "COMVersion.xml"
    xml.write_text('<COMVersion><var><name>PROG_ID_VER</name><data>latest</data></var></COMVersion>')
    with pytest.raises(ValueError, match="numeric"):
        installation(tmp_path)
    xml.write_text('<COMVersion><var><name>PROG_ID_VER</name><data>66</data></var></COMVersion>')
    result = installation(tmp_path)
    assert result['com_version'] == '66'
    assert not any(row['exists'] for row in result['typelibs'].values())


def test_pe_architecture_reads_machine_not_host_bitness(tmp_path):
    path = tmp_path / "sample.exe"
    data = bytearray(256)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, 0x14c)
    path.write_bytes(data)
    assert pe_architecture(path) == 'x86'
    data[128:132] = b'BAD!'
    path.write_bytes(data)
    with pytest.raises(ValueError, match='signature'):
        pe_architecture(path)


def test_unknown_domain_refused_before_any_creation(tmp_path):
    target = tmp_path / 'must-not-create'
    with pytest.raises(ValueError, match='domain'):
        activate('arbitrary-progid', tmp_path, target)
    assert not target.exists()


def test_unbounded_timeout_refused_before_creation(tmp_path):
    target = tmp_path / 'must-not-create'
    with pytest.raises(ValueError, match='timeout'):
        activate('designer', tmp_path, target, timeout=1000)
    assert not target.exists()


def test_existing_editor_blocks_activation_before_state_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'existing_editor', lambda domain: [1234])
    target = tmp_path / 'must-not-create'
    with pytest.raises(RuntimeError, match='editor_already_running'):
        activate('layout', tmp_path, target)
    assert not target.exists()


@pytest.mark.parametrize('stdout,status', [(b'', 'probe_failed'),
    (b'{"stage":"result","status":"activation_only_pass"}\r\n', 'activation_only_pass')])
def test_empty_success_exit_is_not_activation_success(tmp_path, monkeypatch, stdout, status):
    monkeypatch.setattr(probe, 'existing_editor', lambda domain: [])
    monkeypatch.setattr(probe, 'installation', lambda path: {'com_version': '66'})
    monkeypatch.setattr(probe, 'native_environment', lambda *args: {})
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kw: subprocess.CompletedProcess([], 0, stdout, b''))
    assert activate('layout', tmp_path, tmp_path / 'state')['status'] == status


def test_timeout_preserves_last_completed_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'existing_editor', lambda domain: [])
    monkeypatch.setattr(probe, 'installation', lambda path: {'com_version': '66'})
    monkeypatch.setattr(probe, 'native_environment', lambda *args: {})
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired([], 25, output=b'{"stage":"activate","status":"started"}\n{"incomplete')
    monkeypatch.setattr(probe.subprocess, 'run', timeout)
    result = activate('layout', tmp_path, tmp_path / 'state')
    assert result['status'] == 'timeout'
    assert result['events'] == [{'stage': 'activate', 'status': 'started'}]
    assert result['activation_outcome'] == 'uncertain'
