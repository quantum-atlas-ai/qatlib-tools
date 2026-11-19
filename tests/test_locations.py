from pathlib import Path

from qatlib_env import locations, workspace


def test_user_paths_and_legacy_layers(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "QATLIB_HOME": str(tmp_path / "tools"),
           "XDG_CONFIG_HOME": str(tmp_path / "config")}
    home, root = tmp_path / "home", tmp_path / "tools"
    assert locations.config_paths(env) == (tmp_path / "config/qatlib/env.toml",
                                          home / ".qatlib/env.toml", root / "env.toml")
    old_key = home / ".qatlib/secrets"
    old_key.parent.mkdir(parents=True)
    old_key.touch()
    assert locations.secrets_path(env) == old_key
    assert locations.secrets_path(env, read=False) == root / "secrets"
    assert locations.secrets_path({**env, "QATLAS_R2_KEY_FILE": str(tmp_path / "explicit")}) == tmp_path / "explicit"


def test_existing_pool_lock_is_reused(tmp_path):
    env = {"HOME": str(tmp_path)}
    current, legacy, active = locations.pool_paths(env)
    assert active == current == tmp_path / ".qatlib/state/pool"
    assert legacy == tmp_path / ".cache/qatlib-env/pool"
    legacy.mkdir(parents=True)
    (legacy / "ledger.lock").touch()
    assert locations.pool_paths(env)[2] == legacy
    assert locations.pool_paths({**env, "XDG_STATE_HOME": str(tmp_path / "state")})[2] == tmp_path / "state/qatlib-env/pool"
    assert locations.pool_paths({**env, "XDG_CACHE_HOME": str(tmp_path / "cache")})[2] == tmp_path / "cache/qatlib-env/pool"


def test_path_explain_is_read_only_and_keeps_native_cache(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "LAKE_CACHE_DIR": str(tmp_path / "lake")}
    root = tmp_path / "repo"
    data = workspace.explain_paths(root, env)
    assert data["lake_cache"] == str(tmp_path / "lake")
    assert data["checkpoints"] == str(tmp_path / "lake/checkpoints")
    assert Path(data["retrieve"]["write"]).parent == tmp_path / "home/.qatlib/retrieve"
    assert Path(data["cloud_state"]).parent == tmp_path / "home/.qatlib/state/cloud"
    assert list(tmp_path.iterdir()) == []
    legacy = root / ".qatlib-env/cloud-state.json"
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    assert workspace.explain_paths(root, env)["cloud_state"] == str(legacy)
