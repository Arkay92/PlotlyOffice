"""Content-based release IDs for cached cloud deployments."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'apps' / 'slides' / 'assets'


def asset_version(name):
    return hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()[:16]


def release_id():
    digest = hashlib.sha256()
    for path in sorted([ROOT / 'app.py', *(ROOT / 'plotlyslides').glob('*.py'),
                        *ASSETS.rglob('*.js'), *ASSETS.glob('*.css'),
                        ASSETS / 'studio.html']):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
