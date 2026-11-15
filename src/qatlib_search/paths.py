"""Project data paths, independent of the installed Python package location.

Legacy Dev tools retain their source-checkout default when run from an editable
checkout. In a wheel, ROOT follows the caller's Lake project, never site-packages.
New portable tools should accept an explicit project and call project_root();
bundled resources belong beside their modules (or in importlib.resources).
A project root is not authorization to write Dev planning data from research.
"""
from pathlib import Path


def project_root(project: str | Path | None = None) -> Path:
    """Resolve an explicit directory, or find the caller's nearest Lake project.

    Outside a Lake project return cwd so --help and project-independent commands
    still work. Data-consuming tools must check their own required inputs.
    """
    if project is not None:
        root = Path(project).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"project directory does not exist: {root}")
        return root
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if any((candidate / name).is_file() for name in ("lakefile.toml", "lakefile.lean")):
            return candidate
    return current


def _legacy_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    # Recognize an actual source tree; do not mistake a wheel's lib directory
    # (or a coincidental Lake project containing a venv) for the package owner.
    if (candidate / "src/qatlib_search/paths.py").resolve() == Path(__file__).resolve():
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return project_root()


ROOT = _legacy_root()
