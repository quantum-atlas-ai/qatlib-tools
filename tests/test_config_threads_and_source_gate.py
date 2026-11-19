"""Separate process counts from compiler threads, and select current source gates."""
import json
from pathlib import Path
from unittest.mock import patch

from qatlib_env import resources, workspace


def test_inherited_compiler_threads_never_override_job_count(tmp_path):
    (tmp_path / 'qatlib-env.toml').write_text('[resources]\njobs=1\nlean_threads=2\nmemory_budget="4GiB"\n')
    host = resources.HostResources(64 * 1024**3, 48 * 1024**3, 32, 32, 48 * 1024**3)
    values = {'HOME': str(tmp_path), 'LEAN_NUM_THREADS': '16'}
    config = resources.resolve_config(tmp_path, env=values, probe=host)
    assert config.values['jobs'] == 1
    assert config.values['lean_threads'] == 16
    explicit = resources.resolve_config(tmp_path, env={**values, 'QATLIB_LEAN_THREADS': '2'}, probe=host)
    assert explicit.values['jobs'] == 1
    assert explicit.values['lean_threads'] == 2
    exported = explicit.environment(values)
    assert exported['LEAN_NUM_THREADS'] == '2'
    assert resources.resolve_config(tmp_path, env=exported, probe=host).values['jobs'] == 1


def test_legacy_environment_falls_back_under_the_canonical_source_name(tmp_path):
    from qatlib_env.config import load_config

    base = {"HOME": str(tmp_path), "QATLAS_JOBS": "3", "QATLAS_LEAN_THREADS": "2",
            "LEAN_NUM_THREADS": "4", "QATLAS_HEAVY_PATTERNS": "Heavy"}
    legacy = load_config(tmp_path, base)
    assert legacy.resource_values["jobs"] == 3
    assert legacy.resource_values["lean_threads"] == 2
    assert legacy.sources["jobs"] == "environment:QATLIB_JOBS"
    canonical = load_config(tmp_path, {**base, "QATLIB_JOBS": "1", "QATLIB_HEAVY_PATTERNS": ""})
    assert canonical.resource_values["jobs"] == 1
    assert canonical.resource_values["heavy_patterns"] == []


def test_current_source_import_gate_precedes_legacy_and_all_build_steps(tmp_path):
    for package in ('consumer_lib', 'dev_metadata'):
        path = tmp_path / 'python' / package / 'check_source_imports.py'
        path.parent.mkdir(parents=True)
        path.write_text('# fixture\n')
    (tmp_path / 'lake-manifest.json').write_text(json.dumps({'packages': [{'name': 'Qatlib'}]}))
    (tmp_path / 'python/pyproject.toml').write_text('[project.scripts]\ncheck="consumer_lib.__main__:main"\n')
    dependency = tmp_path / '.lake/packages/Qatlib'
    dependency.mkdir(parents=True)
    (dependency / 'lakefile.toml').write_text('name="Qatlib"\n[[lean_lib]]\nname="Qatlib"\n')
    with patch.object(workspace, 'run') as run:
        workspace._check(tmp_path, {}, ['Consumer.Basic'])
    command = run.call_args_list[0].args[0]
    assert any('consumer_lib/check_source_imports.py' in item for item in command)
    assert '--dependency-repo' in command
    assert run.call_args_list[1].args[0] == ['lake', 'lint']
