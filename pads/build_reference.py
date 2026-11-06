import argparse
from pathlib import Path
from runtime.reference import build_index

p = argparse.ArgumentParser()
p.add_argument('--manual-dir', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
a.output.parent.mkdir(parents=True, exist_ok=True)
print(build_index(a.manual_dir, a.output))
