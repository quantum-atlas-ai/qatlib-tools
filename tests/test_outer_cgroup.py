"""Kernel-file fixtures prove outer limits without running a compiler or systemd."""
from pathlib import Path
from unittest.mock import patch

import pytest

from qatlib_env import workspace
from qatlib_env.backends import linux
from qatlib_env.backends.linux import enclosing_cgroup_limits


@pytest.fixture
def service(tmp_path):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/system.slice/runner.service/child\n")
    root = tmp_path / "cgroup"
    parent = root / "system.slice/runner.service"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "memory.max").write_text(str(12 * 1024**3))
    (parent / "memory.current").write_text(str(2 * 1024**3))
    (parent / "cpu.max").write_text("400000 100000\n")
    (child / "memory.max").write_text("max\n")
    (child / "cpu.max").write_text("max 100000\n")
    return proc, root, parent, child


def inspect(service, *, memory_mib=4096, cpu=2):
    proc, root, _, _ = service
    return enclosing_cgroup_limits(memory_mib=memory_mib, cpu=cpu,
                                   proc_root=proc, cgroup_root=root)


def test_service_protection_is_inherited_and_replaces_only_missing_user_scope(service):
    result = inspect(service)
    assert result['memory_max'] == 12 * 1024**3
    assert result['memory_remaining'] == 10 * 1024**3
    assert result['cpu_quota'] == 4
    with patch.object(linux, 'systemd_available', return_value=False), \
         patch.object(linux, 'enclosing_cgroup_limits', side_effect=lambda **kw: inspect(service, **kw)):
        command = ['lake', 'build']
        assert workspace._backend_command(command, {'QATLIB_JOBS': '1', 'QATLIB_LEAN_THREADS': '2'},
                                          building=True, require_limits=True) == command


def test_tightest_ancestor_and_live_remaining_memory_are_enforced(service):
    _, _, parent, child = service
    (child / 'memory.max').write_text(str(6 * 1024**3))
    (child / 'memory.current').write_text(str(1024**3))
    assert inspect(service)['memory_max'] == 6 * 1024**3
    # Parent's other children can consume the headroom after an earlier probe.
    (parent / 'memory.current').write_text(str(10 * 1024**3))
    with pytest.raises(ValueError, match='headroom exceeded'):
        inspect(service)
    (parent / 'memory.current').write_text('0')
    with pytest.raises(ValueError, match='memory limit/headroom exceeded'):
        inspect(service, memory_mib=8192)


def test_parent_cpu_quota_can_protect_memory_limited_child(service):
    _, root, parent, _ = service
    (parent / 'cpu.max').write_text('max 100000')
    (root / 'cpu.max').write_text('150000 100000')
    assert inspect(service, cpu=1)['cpu_quota'] == 1.5
    with pytest.raises(ValueError, match='CPU quota exceeded'):
        inspect(service, cpu=2)


@pytest.mark.parametrize('controller', ['memory.max', 'cpu.max'])
def test_both_hard_controllers_are_required_not_soft_limits_or_affinity(service, controller):
    _, _, parent, _ = service
    (parent / controller).write_text('max 100000' if controller == 'cpu.max' else 'max')
    (parent / 'memory.high').write_text(str(8 * 1024**3))
    (parent / 'cpuset.cpus.effective').write_text('0-1')
    assert inspect(service) is None


@pytest.mark.parametrize('membership', ['0::/missing', '0::/../escape', '1:cpu:/legacy',
                                       '0::/system.slice\n0::/duplicate', '0::relative'])
def test_unknown_or_unsafe_membership_cannot_grant_protection(service, membership):
    proc, _, _, _ = service
    (proc / 'self/cgroup').write_text(membership)
    assert inspect(service) is None


def test_symlink_escape_and_malformed_controller_fail_closed(service, tmp_path):
    proc, root, parent, _ = service
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    (proc / 'self/cgroup').write_text('0::/escape')
    assert inspect(service) is None
    (proc / 'self/cgroup').write_text('0::/system.slice/runner.service/child')
    (parent / 'cpu.max').write_text('broken')
    assert inspect(service) is None


def test_unbounded_developer_session_still_rejects_heavy_execution(service):
    _, _, parent, _ = service
    (parent / 'memory.max').write_text('max')
    (parent / 'cpu.max').write_text('max 100000')
    with patch.object(linux, 'systemd_available', return_value=False), \
         patch.object(linux, 'enclosing_cgroup_limits', side_effect=lambda **kw: inspect(service, **kw)):
        with pytest.raises(ValueError, match='systemd'):
            workspace._backend_command(['lake', 'build'], {}, building=True, require_limits=True)
    with pytest.raises(ValueError, match='controller'):
        workspace._backend_command(['lake', 'build'], {'QATLIB_USE_SYSTEMD_SCOPE': '0'},
                                   building=True, require_limits=True)
