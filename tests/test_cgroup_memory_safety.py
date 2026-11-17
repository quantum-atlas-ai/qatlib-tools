"""Admission must retain real charges and fail closed on incomplete v2 data."""
from unittest.mock import patch

import pytest

from qatlib_env import resources

GIB = resources.GIB


@pytest.fixture
def probe(tmp_path):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text(f"MemTotal: {100 * GIB}\nMemAvailable: {90 * GIB}\n")
    (proc / "self/cgroup").write_text("0::/parent/job\n")
    root = tmp_path / "cgroup"
    job = root / "parent/job"
    job.mkdir(parents=True)

    def run():
        return resources.probe_resources(proc_root=proc, cgroup_root=root,
                                         cpu_count=4, affinity={0, 1, 2, 3})

    return proc, root, job, run


def limits(directory, high="max", maximum="max", current="0"):
    for name, value in (("memory.high", high), ("memory.max", maximum), ("memory.current", current)):
        (directory / name).write_text(str(value))


@pytest.mark.parametrize("current", [None, "", "-1", "oops", "1.5", "１２"])
def test_unknown_usage_does_not_grant_finite_limit(probe, current):
    _, _, job, run = probe
    (job / "memory.high").write_text(str(40 * GIB))
    if current is not None:
        (job / "memory.current").write_text(current)
    host = run()
    assert host.memory_headroom == 0
    assert host.memory_budget == 1  # positive config floor; no executable request fits
    assert "memory.error" in host.limits["cgroups"][0]


@pytest.mark.parametrize("name", ["memory.max", "memory.high"])
@pytest.mark.parametrize("value", ["", "-1", "bad", "1.0", "inf"])
def test_bad_limit_does_not_remove_an_ancestor_bound(probe, name, value):
    _, root, job, run = probe
    limits(job, high=20 * GIB)
    (root / name).write_text(value)
    assert run().memory_headroom == 0


def test_ancestor_minimum_and_host_reserve_are_independent(probe):
    proc, root, job, run = probe
    limits(job, high=40 * GIB, maximum=50 * GIB, current=8 * GIB)
    limits(root / "parent", high=35 * GIB, maximum=25 * GIB, current=10 * GIB)
    limits(root, high=60 * GIB, current=48 * GIB)
    assert run().memory_headroom == 12 * GIB
    (proc / "meminfo").write_text(f"MemTotal: {100 * GIB}\nMemAvailable: {16 * GIB}\n")
    host = run()
    assert host.reserve == 10 * GIB
    assert host.memory_budget == 6 * GIB


@pytest.mark.parametrize("stat", ["", "inactive_file invalid\n", "anon 0\nfile 38654705664\ninactive_file 38654705664\n",
                                "anon 38654705664\nfile_dirty 4294967296\nfile_writeback 4294967296\n"])
def test_file_cache_and_anonymous_charges_are_not_discounted(probe, stat):
    _, _, job, run = probe
    limits(job, high=40 * GIB, maximum=44 * GIB, current=36 * GIB)
    (job / "memory.stat").write_text(stat)
    assert run().memory_headroom == 4 * GIB


@pytest.mark.parametrize("current", [40 * GIB, 45 * GIB])
def test_at_or_above_high_has_no_admission_capacity(probe, current):
    _, _, job, run = probe
    limits(job, high=40 * GIB, maximum=44 * GIB, current=current)
    assert run().memory_headroom == 0


def test_zero_is_a_finite_limit_and_max_is_unbounded(probe):
    _, _, job, run = probe
    limits(job)
    assert run().memory_headroom == 100 * GIB
    limits(job, high=0)
    assert run().memory_headroom == 0


@pytest.mark.parametrize("failure", [FileNotFoundError(), PermissionError()])
def test_usage_disappearing_or_becoming_unreadable_fails_closed(probe, failure):
    _, _, job, run = probe
    limits(job, high=40 * GIB)
    read = resources._read

    def racing_read(path):
        if path == job / "memory.current":
            raise failure
        return read(path)

    with patch.object(resources, "_read", side_effect=racing_read):
        assert run().memory_headroom == 0
