"""Public-interface smoke reports real failures; rename records never hide them."""
import subprocess
import os
import sys
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from qatlib_env import smoke


def probe(root, source="module\nimport Provider.Small\ndef computed : Nat := Provider.value\n"):
    path = root / "Consumer.lean"
    path.write_text(source)
    return path


@pytest.mark.parametrize("source", [
    "import Provider.Small\n", "module\nimport all Provider.Small\n",
    "module\nimport Provider.Small\nnoncomputable def hidden := Provider.value\n",
    "module\nimport Provider.Small\nexample : False := by sorry\n"])
def test_probes_cannot_accidentally_bypass_the_public_contract(tmp_path, source):
    with pytest.raises(ValueError):
        smoke.validate_probe(tmp_path, probe(tmp_path, source))


def test_comments_do_not_change_the_probe_contract(tmp_path):
    source = '/- noncomputable; import all Fake -/\nmodule\nimport Provider.Small\n#eval "sorry"\n'
    assert smoke.validate_probe(tmp_path, probe(tmp_path, source)).is_file()


def test_smoke_preserves_failure_and_mapping_for_review(tmp_path):
    path = probe(tmp_path)
    mapping = tmp_path / "migration.toml"
    mapping.write_text('[[rename]]\nold="Provider.old"\nnew="Provider.value"\nreason="intentional API cleanup"\n')
    with patch.object(smoke.workspace, "environment", return_value={}), \
            patch.object(smoke.workspace, "run", side_effect=subprocess.CalledProcessError(
                1, ["lake"], output="unknown constant Provider.old")):
        result = smoke.run(tmp_path, [path], mapping)
    assert result["probes"][0]["status"] == "REGRESSION"
    assert "unknown constant" in result["probes"][0]["diagnostics"]
    assert result["renames"][0]["reason"] == "intentional API cleanup"
    assert result["errors"]


def test_budget_or_toolchain_failure_is_not_reported_as_pass(tmp_path):
    path = probe(tmp_path)
    with patch.object(smoke.workspace, "environment", side_effect=ValueError("budget unavailable")):
        result = smoke.run(tmp_path, [path])
    assert result["probes"][0]["status"] == "NOT-RUN"


def test_success_runs_only_the_small_file_without_dependency_build(tmp_path):
    path = probe(tmp_path)
    with patch.object(smoke.workspace, "environment", return_value={}), \
            patch.object(smoke.workspace, "run", return_value="value: 7") as run:
        result = smoke.run(tmp_path, [path])
    assert result["probes"][0]["status"] == "PASS"
    assert not result["errors"]
    assert run.call_args.args[0] == ["lake", "--no-build", "lean", str(path)]
    assert run.call_args.kwargs["capture_stderr"] is True


def test_mapping_requires_reason_and_unique_sources(tmp_path):
    path = tmp_path / "migration.toml"
    path.write_text('[[rename]]\nold="Provider.old"\nnew="Provider.new"\n')
    with pytest.raises(ValueError, match="reason"):
        smoke.migration_map(path)


def test_supervised_stderr_only_failure_is_available_to_smoke(tmp_path):
    # Keep the actual supervisor and subprocess; isolate unrelated build admission.
    from qatlib_env import workspace
    with patch('qatlib_env.budget.command_request', return_value=(False, [], False)), \
            patch.object(workspace, 'command_runtime_environment', side_effect=lambda p, e, a: e), \
            patch.object(workspace, '_backend_command', side_effect=lambda a, *args, **kwargs: a), \
            patch.object(workspace, '_pool_scope', return_value=nullcontext()):
        with pytest.raises(subprocess.CalledProcessError) as failure:
            workspace.run([sys.executable, '-c',
                           'import sys; sys.stderr.write("missing public interface\\n"); sys.exit(3)'],
                          cwd=tmp_path, env=dict(os.environ), capture=True, capture_stderr=True)
    assert failure.value.returncode == 3
    assert 'missing public interface' in failure.value.stdout


def test_promotion_preserves_global_resource_and_timeout_overrides(tmp_path, capsys):
    from qatlib_env.workspace import main
    values = {'QATLAS_JOBS': '1', 'QATLAS_MEMORY_BUDGET': '2147483648',
              'QATLAS_STAGE_TIMEOUT': '2', 'QATLAS_MODULE_TIMEOUT': '1'}
    with patch.object(smoke.workspace, 'environment', return_value=values) as environment, \
            patch.object(smoke, 'run', return_value={'errors': []}) as run:
        assert main(['--project', str(tmp_path), '--jobs', '1', '--memory-budget', '2GiB',
                     '--stage-timeout', '2', '--module-timeout', '1',
                     'promotion', 'smoke', '--probe', 'Consumer.lean']) == 0
    assert environment.call_args.kwargs['cli'] == {
        'jobs': '1', 'memory_budget': '2GiB', 'stage_timeout': '2', 'module_timeout': '1'}
    assert run.call_args.kwargs['env'] == values


def test_smoke_keeps_the_callers_resolved_budget(tmp_path):
    path = probe(tmp_path)
    values = {'QATLAS_STAGE_TIMEOUT': '2', 'QATLAS_JOBS': '1'}
    with patch.object(smoke.workspace, 'environment', return_value=values) as environment, \
            patch.object(smoke.workspace, 'run', return_value='') as run:
        assert not smoke.run(tmp_path, [path], env=values)['errors']
    environment.assert_called_once_with(tmp_path, base_env=values)
    assert run.call_args.kwargs['env'] == values
