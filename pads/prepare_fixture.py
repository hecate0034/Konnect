"""Copy the installed evaluation project and library; never edit originals."""
import argparse
import json
import shutil
from pathlib import Path
from runtime.diagnostics import fingerprint


def prepare(source: Path, target: Path):
    source = source.resolve(strict=True)
    target = target.resolve()
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('fixture destination must be new and separate from the source')
    manifest = {}
    for folder in ('LessonFinal', 'Library'):
        for path in (source / folder).rglob('*'):
            if path.is_symlink():
                raise ValueError('source links are not supported')
            if path.is_file():
                manifest[str(path.relative_to(source))] = fingerprint(path)
    target.mkdir(parents=True)
    for folder in ('LessonFinal', 'Library'):
        shutil.copytree(source / folder, target / folder)
    changes = []
    # Configuration path relocation is confined to copied text files.
    for path in target.rglob('*'):
        if path.suffix.lower() not in ('.prj', '.ini', '.dbc', '.ipl'):
            continue
        original = path.read_bytes()
        if b'\x00' in original:
            continue  # Never perform byte substitution on binary native objects.
        updated = original.replace(b'C:\\Pads_Professional_Eval', str(target).encode('ascii'))
        updated = updated.replace(b'C:/Pads_Professional_Eval', target.as_posix().encode('ascii'))
        if updated != original:
            path.write_bytes(updated)
            changes.append(str(path.relative_to(target)))
    for relative, sha in manifest.items():
        if fingerprint(source / relative) != sha:
            raise ValueError('source changed during fixture preparation')
    (target / 'source-manifest.json').write_text(json.dumps({'source':str(source), 'files':manifest,
        'relocated_configuration_files':changes}, indent=2), encoding='utf-8')
    return {'project': str(target / 'LessonFinal/LessonFinal.prj'),
            'pcb': str(target / 'LessonFinal/PCB/CORPORATE.pcb'), 'source_files':len(manifest),
            'relocated_files':len(changes)}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--target', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.source, a.target)))
