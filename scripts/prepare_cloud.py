"""Build the self-contained Plotly Cloud deployment bundle in deploy/plotly-cloud/."""
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
BUNDLE = DEPLOY / "plotly-cloud"
ARCHIVE = DEPLOY / "plotlyoffice-plotly-cloud.zip"

INCLUDE_FILES = [
    "app.py",
    "main.py",
    "requirements.txt",
    "LICENSE",
    "README.md",
]

INCLUDE_DIRS = [
    "assets",
    "apps",
    "plotlyoffice",
    "plotlysheets",
    "plotlyslides",
    "plotlyword",
]

IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
)


def main() -> None:
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True, exist_ok=True)

    for rel in INCLUDE_FILES:
        src = ROOT / rel
        if src.exists():
            shutil.copy2(src, BUNDLE / rel)

    for rel in INCLUDE_DIRS:
        src = ROOT / rel
        if src.is_dir():
            shutil.copytree(src, BUNDLE / rel, ignore=IGNORE_PATTERNS)

    if ARCHIVE.exists():
        ARCHIVE.unlink()

    with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(BUNDLE.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(BUNDLE))

    print(f"Built {BUNDLE}")
    print(f"Built {ARCHIVE}")


if __name__ == "__main__":
    main()
