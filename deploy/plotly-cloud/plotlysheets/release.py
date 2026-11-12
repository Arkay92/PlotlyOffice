"""Content-derived build identifiers for safe asset caching."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "apps" / "sheets" / "assets"


def asset_version(name):
    return hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()[:16]


def release_id():
    digest = hashlib.sha256()
    paths = [
        ROOT / "app.py",
        *(ROOT / "plotlysheets").glob("*.py"),
        *ASSETS.glob("*.js"),
        *ASSETS.glob("*.css"),
        ASSETS / "studio.html",
    ]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
