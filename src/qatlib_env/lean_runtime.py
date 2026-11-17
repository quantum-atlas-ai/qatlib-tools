"""Read-only preflight for explicitly requested Lean retrieval probes."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_env.git_identity import assert_clean_git_sources


def prepared_environment(root: Path, base_env: dict[str, str] | None = None,
                         temporary: Path | None = None) -> dict[str, str]:
    """Select the workflow's pool without creating directories or dependencies."""
    environment = dict(os.environ if base_env is None else base_env)
    project = Path(root).resolve()
    from qatlib_env import workspace as workflow
    environment['LAKE_CACHE_DIR'] = str(workflow.cache_directory(project, environment))
    environment.setdefault('LAKE_ARTIFACT_CACHE', 'true')
    environment.setdefault('MATHLIB_NO_CACHE_ON_UPDATE', '1')
    if temporary is not None:
        for variable in ['TMPDIR', 'TMP', 'TEMP']:
            environment[variable] = str(temporary)
    return environment


def require_prepared_dependencies(root: Path) -> None:
    root = Path(root).resolve()
    try:
        manifest = json.loads((root / 'lake-manifest.json').read_text())
        if (root / manifest.get('lakeDir', '.lake') / 'package-overrides.json').exists():
            raise ValueError('package overrides can replace locked dependency pins')
        packages = manifest['packages']
        for package in packages:
            name = package['name'].strip('«»')
            if package['type'] != 'git' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
                raise ValueError('only named, locked Git dependencies are supported')
            checkout = root / manifest.get('packagesDir', '.lake/packages') / name
            assert_clean_git_sources(checkout, expected_head=package['rev'])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit(f'Dependencies are not prepared: {error}. '
                         'Prepare locked sources/cache explicitly; no dependencies were installed or updated.') from None
    # Each real probe uses `lake --no-build lean <driver>` to validate its actual
    # import closure. Do not traverse the entire library aggregate for every query.

