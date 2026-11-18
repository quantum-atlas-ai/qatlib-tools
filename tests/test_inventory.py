"""Complete preview warnings, explicit approval and runtime boundaries."""
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qatlib_env import inventory
from tests._git_fixture import snapshot


def fact(path, **changes):
    row = {"path": str(path), "errors": [], "warnings": [], "head": "a" * 40,
           "identity": [1, 2, 3, 4], "git_dir": "/common", "common_dir": "/common",
           "linked_worktree": False, "last_activity": 1, "dirty_count": 0,
           "dirty_entries": [], "ignored_paths": [], "ignored_count": 0}
    row.update(changes)
    return row


def group(**changes):
    row = {"errors": [], "stash_count": 0, "worktrees": [], "remote_verified": True,
           "unpushed_branches": [], "unknown_branches": []}
    row.update(changes)
    return row


def mock_cleanup(monkeypatch, *, facts=None, groups=None, active=None, warnings=()):
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(inventory, "checkout_facts", facts or (lambda p, **k: fact(p)))
    monkeypatch.setattr(inventory, "common_facts", groups or (lambda *a, **k: group()))
    monkeypatch.setattr(inventory, "processes", lambda: (active or [], []))
    monkeypatch.setattr(inventory, "_lake_warnings", lambda *a, **k: list(warnings))
    monkeypatch.setattr(inventory, "_read_git", lambda *a: "")
    monkeypatch.setattr(inventory, "_identity", lambda p: [1, 2, 3, 4])
    monkeypatch.setattr(inventory.system_trash, "measure", lambda p: {"apparent_bytes": 5, "allocated_bytes": 8})
    monkeypatch.setattr(inventory.system_trash, "probe", lambda p: {"supported": True, "can_trash": None})
    moved = Mock(side_effect=lambda p: {"original_path": str(p), "backend": "send2trash.native"})
    monkeypatch.setattr(inventory.system_trash, "trash", moved)
    return moved


def test_discover_roots_markers_and_links(monkeypatch):
    trees = {"/root": [("/root", ["one", "link", ".git", ".lake"], ["lakefile.toml"]),
                      ("/root/one", ["nested"], [".git"]),
                      ("/root/one/nested", ["questions"], [".git"])],
             "/elsewhere": [("/elsewhere", [], ["theorem.lean"])]}
    def walk(root, **kw):
        for path, dirs, files in trees[str(root)]:
            yield path, list(dirs), files
    monkeypatch.setattr(inventory.os, "walk", walk)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_dev=1))
    monkeypatch.setattr(Path, "is_symlink", lambda self: str(self) == "/root/link")
    repos, markers, errors, skipped = inventory.discover(["/root", "/elsewhere"])
    assert repos == ["/root", "/root/one", "/root/one/nested"]
    assert {"path": "/elsewhere", "markers": ["lean-source"]} in markers
    assert skipped == [{"path": "/root/link", "reason": "symlink"}]
    assert not errors


def test_exact_scan_groups_common_dir_without_writes(monkeypatch):
    monkeypatch.setattr(inventory, "checkout_facts", lambda p, **kw: fact(p))
    common = Mock(return_value=group())
    monkeypatch.setattr(inventory, "common_facts", common)
    monkeypatch.setattr(inventory, "processes", lambda: ([], []))
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    def forbidden(*a, **kw):
        raise AssertionError("read-only preview tried to write")
    for name in ("write_text", "write_bytes", "mkdir", "rename", "replace", "unlink", "rmdir"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(inventory.system_trash, "trash", forbidden)
    result = inventory.scan(["/r/a", "/r/a", "/r/b"], exact=True, now=100000)
    assert [r["path"] for r in result["checkouts"]] == ["/r/a", "/r/b"]
    assert common.call_count == 1


def test_scan_cli_reports_directory_truncation(tmp_path, capsys):
    import json

    (tmp_path / "nested").mkdir()
    assert inventory.cli_main(["scan", str(tmp_path), "--max-directories", "1"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["truncated"] is True
    assert result["errors"] == [f"directory limit 1 reached at {tmp_path / 'nested'}; narrow roots"]


def test_derived_cleanup_selects_only_build_and_packages(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    result = inventory.clean(["/r/a"], mode="derived", apply=True)
    assert result["applied"]
    assert [str(call.args[0]) for call in moved.call_args_list] == [
        "/r/a/.lake/build", "/r/a/.lake/packages"]


def test_git_observation_disables_locks_fetch_and_prompts(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(inventory.subprocess, "run", run)
    inventory.git("/r/a", "status")
    env = run.call_args.kwargs["env"]
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("flag", ["H", "h", "S", "s"])
def test_hidden_git_flags_are_warnings_and_paths_are_not_truncated(monkeypatch, flag):
    sha = "a" * 40
    states = [f"?? file-{i}" for i in range(25)]
    def read(path, *args):
        if args == ("rev-parse", "HEAD"): return sha
        if args == ("rev-parse", "--show-toplevel"): return "/r/a"
        if args == ("branch", "--show-current"): return "topic"
        if args == ("rev-parse", "--absolute-git-dir"): return "/common/worktrees/a"
        if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"): return "/common"
        if args == ("ls-files", "-v", "-z"): return flag + " tracked\0"
        if args[0] == "status": return "\0".join(states) + "\0"
        # Git --directory may still collapse an excluded .lake to this entry.
        if args[:3] == ("ls-files", "--others", "--ignored"): return ".lake/\0ignored-task/\0"
        if args[0] == "log": return "1"
        raise AssertionError(args)
    monkeypatch.setattr(inventory, "_read_git", read)
    monkeypatch.setattr(inventory, "git", lambda *a, **k: SimpleNamespace(returncode=0, stdout="origin", stderr=""))
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(
        st_dev=1, st_ino=2, st_mtime_ns=3, st_ctime_ns=4, st_mtime=1))
    row = inventory.checkout_facts("/r/a", inspect_ignored=True)
    assert not row["errors"]
    assert bool(row["warnings"]) is (flag != "H")
    assert len(row["dirty_entries"]) == 25
    assert row["ignored_paths"] == ["ignored-task/"]


@pytest.mark.parametrize("kind", ["hot", "dirty", "ignored", "stash", "unknown", "unpushed", "inspection_error"])
@pytest.mark.parametrize("mode", ["caches", "checkouts"])
def test_data_findings_are_warnings_and_apply_confirms_them(monkeypatch, kind, mode):
    changes = {}
    g = group()
    if kind == "hot": changes["last_activity"] = inventory.time.time()
    if kind == "dirty": changes.update(dirty_count=1, dirty_entries=[" M changed"])
    if kind == "ignored": changes.update(ignored_count=1, ignored_paths=["ignored-task/"])
    if kind == "stash": g["stash_count"] = 1
    if kind == "unknown": g.update(remote_verified=False, unknown_branches=[{"ref": "topic", "sha": "b"*40}])
    if kind == "unpushed": g["unpushed_branches"] = [{"ref": "topic", "sha": "b"*40}]
    if kind == "inspection_error": changes["errors"] = ["Git inspection timed out"]
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(p, **changes), groups=lambda *a, **k: g)
    result = inventory.clean(["/r/a"], mode=mode, apply=True)
    assert result["applied"]
    assert result["targets"][0]["eligible"]
    assert result["targets"][0]["warnings"]
    assert not result["targets"][0]["reasons"]
    moved.assert_called_once()


def test_active_build_blocks_entire_batch(monkeypatch):
    moved = mock_cleanup(monkeypatch, active=[{"pid": 7, "cwd": "/r/b/build", "command": ["ninja"]}])
    result = inventory.clean(["/r/a", "/r/b"], apply=True)
    assert "batch refused" in result["error"]
    assert "active" in " ".join(result["targets"][1]["reasons"])
    moved.assert_not_called()


def test_parent_build_cwd_alone_does_not_claim_nested_checkout():
    process = {"pid": 7, "cwd": "/r/a", "command": ["lean", "Main.lean"]}
    assert not inventory._active_for(Path("/r/a/nested"), [process])
    process["command"].append("/r/a/nested/Probe.lean")
    assert inventory._active_for(Path("/r/a/nested"), [process])


@pytest.mark.parametrize("argument", ["repo/File.lean", "-Irepo/include"])
def test_relative_build_arguments_are_resolved_against_process_cwd(argument):
    process = {"pid": 7, "cwd": "/example", "command": ["lean", argument]}
    assert inventory._active_for(Path("/example/repo"), [process])


def test_new_build_before_move_is_refused(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, "processes", Mock(side_effect=[
        ([], []), ([{"pid": 8, "cwd": "/r/a", "command": ["lake"]}], [])]))
    result = inventory.clean(["/r/a"], apply=True)
    assert "active" in result["error"]
    moved.assert_not_called()


def test_preview_never_calls_trash_even_when_warnings_are_accepted(monkeypatch):
    moved = mock_cleanup(monkeypatch, warnings=[{"path": "/r/a/.lake/task-416", "reason": "nonstandard Lake content"}])
    result = inventory.clean(["/r/a"])
    assert result["targets"][0]["eligible"]
    assert result["targets"][0]["warnings"]
    assert not result["applied"] and not result["receipts"]
    moved.assert_not_called()


def test_unknown_process_details_are_visible_warnings(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, "processes", lambda: ([], ["process inspection incomplete"]))
    result = inventory.clean(["/r/a"], apply=True)
    assert result["applied"] and result["targets"][0]["warnings"]
    moved.assert_called_once()


def test_nonstandard_cache_and_dirty_dependency_report_exact_paths(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    lake = checkout / ".lake"
    (lake / "task-416").mkdir(parents=True)
    (lake / "task-416/result.json").write_text("research")
    package = lake / "packages/example"
    (package / ".git").mkdir(parents=True)
    (package / "changed.lean").write_text("modified")
    (package / "ignored-task").mkdir()
    (package / "ignored-task/result.csv").write_text("data")
    def facts(path, **kw):
        return fact(path, dirty_count=1, dirty_entries=[" M changed.lean"],
                    ignored_paths=["ignored-task/"]) if Path(path) == package else fact(path)
    monkeypatch.setattr(inventory, "checkout_facts", facts)
    monkeypatch.setattr(inventory, "common_facts", lambda *a, **k: group())
    monkeypatch.setattr(inventory, "processes", lambda: ([], []))
    monkeypatch.setattr(inventory, "_read_git", lambda *a: ".lake/task-416/result.json\0")
    moved = Mock(return_value={"backend": "send2trash.native"})
    monkeypatch.setattr(inventory.system_trash, "trash", moved)
    result = inventory.clean([checkout], apply=True)
    row = result["targets"][0]
    assert result["applied"] and row["eligible"] and not row["reasons"]
    warnings = row["warnings"]
    assert any(w["path"] == str(lake / "task-416") and w["apparent_bytes"] >= 8 for w in warnings)
    assert any(w["path"] == str(package / "changed.lean") for w in warnings)
    assert any(w["path"] == str(package / "ignored-task") for w in warnings)
    assert any(w["reason"] == "Lake content is tracked by the checkout" for w in warnings)
    moved.assert_called_once_with(lake)


def test_real_approved_synthetic_cache_uses_recoverable_trash(tmp_path, monkeypatch):
    import os
    if os.name == "nt" or inventory.sys.platform == "darwin":
        pytest.skip("isolated FreeDesktop receipt probe")
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".lake/task-416").mkdir(parents=True)
    (checkout / ".lake/task-416/result.txt").write_text("preserved payload")
    monkeypatch.setattr(inventory, "processes", lambda: ([], []))
    native = inventory.system_trash.trash
    xdg = tmp_path / "xdg"
    monkeypatch.setattr(inventory.system_trash, "trash",
                        lambda path: native(path, {"HOME": str(tmp_path), "XDG_DATA_HOME": str(xdg)}))
    result = inventory.clean([checkout], apply=True)
    assert result["applied"] and result["receipts"]
    assert not (checkout / ".lake").exists()
    assert (xdg / "Trash/files/.lake/task-416/result.txt").read_text() == "preserved payload"
    assert (xdg / "Trash/info/.lake.trashinfo").is_file()


@pytest.mark.parametrize("path", ["/", str(Path.home())])
def test_filesystem_and_home_roots_are_out_of_scope(monkeypatch, path):
    moved = mock_cleanup(monkeypatch)
    result = inventory.clean([path], mode="checkouts", apply=True)
    assert not result["targets"][0]["eligible"]
    moved.assert_not_called()


def test_symlink_escape_is_refused(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / ".lake").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(inventory, "processes", lambda: ([], []))
    moved = Mock()
    monkeypatch.setattr(inventory.system_trash, "trash", moved)
    result = inventory.clean([checkout], apply=True)
    assert "symlinked" in " ".join(result["targets"][0]["reasons"])
    assert outside.exists()
    moved.assert_not_called()


def test_moving_common_dir_cannot_break_an_unselected_checkout(monkeypatch):
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(p, common_dir="/r/a/.git"),
                         groups=lambda *a, **k: group(worktrees=["/r/a", "/outside"]))
    result = inventory.clean(["/r/a"], mode="checkouts", apply=True)
    assert "outside the selected target" in " ".join(result["targets"][0]["reasons"])
    moved.assert_not_called()


def test_all_selected_worktrees_can_move_with_primary_last(monkeypatch):
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(
        p, common_dir="/r/a/.git", linked_worktree=str(p).endswith("/b")),
        groups=lambda *a, **k: group(worktrees=["/r/a", "/r/b"]))
    result = inventory.clean(["/r/a", "/r/b"], mode="checkouts", apply=True)
    assert result["applied"]
    assert result["worktree_prunes"] == []
    assert [call.args[0] for call in moved.call_args_list] == [Path("/r/b"), Path("/r/a")]


@pytest.mark.parametrize("prune", [True, False])
def test_trash_linked_worktree_prunes_by_default(tmp_path, monkeypatch, capsys, prune):
    import json

    main, topic, trashed = (tmp_path / name for name in ("main", "topic", "trashed"))
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    snapshot(main)
    inventory._read_git(main, "worktree", "add", "--detach", str(topic))
    monkeypatch.setattr(inventory, "processes", lambda: ([], []))
    monkeypatch.setattr(inventory.system_trash, "probe", lambda p: {"supported": True})

    def trash(path):
        path.rename(trashed)
        return {"original_path": str(path)}

    monkeypatch.setattr(inventory.system_trash, "trash", trash)
    args = ["trash-checkouts", "--checkout", str(topic)]
    assert inventory.cli_main(args + ["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["worktree_prunes"] == []
    assert topic.is_dir()
    assert inventory.cli_main(args + ["--apply"] + ([] if prune else ["--no-prune"])) == 0
    result = json.loads(capsys.readouterr().out)
    assert not topic.exists()
    assert (trashed / ".git").is_file()
    assert result["applied"] is True
    registered = inventory._read_git(main, "worktree", "list", "--porcelain")
    assert (topic.as_posix() in registered) is not prune
    assert inventory._read_git(main, "rev-parse", "HEAD").strip()
    assert result["worktree_prunes"] == ([{"common_dir": str((main / ".git").resolve()),
                                          "pruned": True}] if prune else [])


def test_prune_failure_keeps_successful_trash_receipt(monkeypatch):
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(p, linked_worktree=True))
    monkeypatch.setattr(inventory, "_read_git", Mock(side_effect=ValueError("prune failed")))
    result = inventory.clean(["/r/a"], mode="checkouts", apply=True)
    assert result["applied"] is True
    moved.assert_called_once_with(Path("/r/a"))
    assert result["receipts"] == [{"original_path": "/r/a", "backend": "send2trash.native"}]
    assert result["worktree_prunes"][0]["pruned"] is False
    assert "worktree prune failed" in result["error"]


def test_new_unselected_worktree_registration_refuses_before_move(monkeypatch):
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(p, common_dir="/r/a/.git"),
                         groups=lambda *a, **k: group(worktrees=["/r/a"]))
    monkeypatch.setattr(inventory, "_read_git", lambda path, *args:
                        "worktree /outside\0" if args[0] == "worktree" else "")
    result = inventory.clean(["/r/a"], mode="checkouts", apply=True)
    assert "outside the selected targets" in result["error"]
    moved.assert_not_called()


def test_backend_unavailable_refuses(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory.system_trash, "probe", Mock(side_effect=RuntimeError("Trash unsupported mount")))
    result = inventory.clean(["/r/a"], apply=True)
    assert not result["targets"][0]["eligible"]
    moved.assert_not_called()


def test_changed_target_identity_refuses(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, "_identity", Mock(side_effect=[[1, 2, 3, 4], [1, 9, 3, 4]]))
    result = inventory.clean(["/r/a"], apply=True)
    assert "target changed" in result["error"]
    moved.assert_not_called()


def test_partial_receipts_survive_disappearing_target(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, "_identity", Mock(side_effect=[
        [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4], FileNotFoundError("gone")]))
    result = inventory.clean(["/r/a", "/r/b"], apply=True)
    assert len(result["receipts"]) == 1 and "unavailable" in result["error"]
    assert moved.call_count == 1


@pytest.mark.parametrize("paths", [["/r/a", "/r/other/../a"], ["/r/a", "/r/a/nested"]])
def test_duplicate_or_overlapping_checkout_selection_refuses(monkeypatch, paths):
    moved = mock_cleanup(monkeypatch)
    with pytest.raises(ValueError):
        inventory.clean(paths, mode="checkouts", apply=True)
    moved.assert_not_called()


def test_unknown_mode_refuses(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    with pytest.raises(ValueError, match="mode must be"):
        inventory.clean(["/r/a"], mode="cache", apply=True)
    moved.assert_not_called()


def test_cli_explicit_apply_and_markers(monkeypatch, capsys):
    clean = Mock(return_value={"applied": False, "targets": []})
    scan = Mock(return_value={"checkouts": []})
    monkeypatch.setattr(inventory, "clean", clean)
    monkeypatch.setattr(inventory, "scan", scan)
    assert inventory.cli_main(["clean-caches", "--checkout", "/r/a", "--dry-run"]) == 0
    assert clean.call_args.kwargs["apply"] is False
    assert inventory.cli_main(["clean-caches", "--checkout", "/r/a", "--apply"]) == 0
    assert clean.call_args.kwargs["apply"] is True
    assert inventory.cli_main(["scan", "--exact", "--marker", "studies", "/r/a"]) == 0
    assert "studies" in scan.call_args.kwargs["semantic"]
    capsys.readouterr()


@pytest.mark.parametrize("hours", [-1, 0, 23.99, float("nan"), float("inf")])
def test_scan_invalid_window(hours):
    with pytest.raises(ValueError, match="at least 24"):
        inventory.scan(["/r"], hot_hours=hours)

@pytest.mark.parametrize(('missing', 'expected'), [(False, 'unpushed_branches'), (True, 'unknown_branches')])
def test_remote_ancestry_and_missing_objects_are_distinguished(monkeypatch, missing, expected):
    local, remote = 'a' * 40, 'b' * 40
    def read(path, *args):
        if args[:2] == ('stash', 'list'): return ''
        if args[:2] == ('worktree', 'list'): return 'worktree /r/a\0HEAD ' + local + '\0'
        if args[0] == 'for-each-ref': return local + ' refs/heads/main\n'
        if args[:2] == ('remote', 'get-url'): return 'https://example.com/org/repo.git\n'
        if args[:2] == ('rev-parse', '--is-shallow-repository'): return 'false\n'
        raise AssertionError(args)
    def git(path, *args, **kwargs):
        if args[0] == 'ls-remote': return SimpleNamespace(returncode=0, stdout=remote + '\trefs/heads/main\n', stderr='')
        if args[0] == 'cat-file':
            return SimpleNamespace(returncode=0, stdout=(remote + ' missing\n' if missing else remote + ' commit\n'), stderr='')
        if args[0] == 'rev-list': return SimpleNamespace(returncode=0, stdout=remote + '\n', stderr='')
        raise AssertionError(args)
    monkeypatch.setattr(inventory, '_read_git', read)
    monkeypatch.setattr(inventory, 'git', git)
    result = inventory.common_facts('/r/a', check_remote=True)
    assert not result['errors']
    assert result[expected]
    assert result['remote_verified'] is (not missing)


@pytest.mark.parametrize('origin', ['https://example.com/org/repo.git', '/local/source'])
def test_remote_ancestor_makes_local_branch_reachable(monkeypatch, origin):
    local, remote = 'a' * 40, 'b' * 40
    def read(path, *args):
        if args[:2] == ('stash', 'list'): return ''
        if args[:2] == ('worktree', 'list'): return 'worktree /r/a\0HEAD ' + local + '\0'
        if args[0] == 'for-each-ref': return local + ' refs/heads/main\n'
        if args[:2] == ('remote', 'get-url'): return origin + '\n'
        if args[:2] == ('rev-parse', '--is-shallow-repository'): return 'false\n'
        raise AssertionError(args)
    def git(path, *args, **kwargs):
        if args[0] == 'ls-remote': output = remote + '\trefs/heads/main\n'
        elif args[0] == 'cat-file': output = remote + ' commit\n'
        elif args[0] == 'rev-list': output = remote + '\n' + local + '\n'
        else: raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(inventory, '_read_git', read)
    monkeypatch.setattr(inventory, 'git', git)
    result = inventory.common_facts('/r/a', check_remote=True)
    assert result['remote_verified'] is (origin.startswith('https:'))
    assert not result['unknown_branches'] and not result['unpushed_branches']
