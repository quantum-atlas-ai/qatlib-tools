"""User-owned qatlib paths, separate from Lake and forge clients' storage."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from qatlib_env.envvars import env_value


def user_home(env=os.environ):
    return Path(env.get("HOME") or Path.home()).expanduser()


def qatlib_home(env=os.environ):
    return Path(env["QATLIB_HOME"]).expanduser().resolve() if env.get("QATLIB_HOME") else user_home(env) / ".qatlib"


def config_paths(env=os.environ):
    home = user_home(env)
    return tuple(dict.fromkeys((home / ".qatlib/env.toml", qatlib_home(env) / "env.toml")))


def secrets_path(env=os.environ, *, read=True):
    explicit = env_value(env, "QATLIB_R2_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    current = qatlib_home(env) / "secrets"
    previous = user_home(env) / ".qatlib/secrets"
    return previous if read and not current.exists() and previous.exists() else current


def pool_paths(env=os.environ):
    current = qatlib_home(env) / "state/pool"
    override = env.get("XDG_STATE_HOME") or env.get("XDG_CACHE_HOME")
    legacy = Path(override or user_home(env) / ".cache").expanduser() / "qatlib-env/pool"
    # Reuse the same lock inode across old and new tools while a legacy pool
    # is in use. An explicit XDG setting retains its established semantics.
    use_legacy = override or any((legacy / name).exists() for name in ("ledger.lock", "ledger.json"))
    return current, legacy, legacy if use_legacy else current


def project_key(project):
    return hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:24]


def index_paths(project, env=os.environ):
    filename = project_key(project) + ".sqlite3"
    old_cache = Path(env.get("XDG_CACHE_HOME") or user_home(env) / ".cache").expanduser()
    return (qatlib_home(env) / "retrieve" / filename,
            old_cache / "qatlib/retrieve" / filename)


def cloud_state_path(project, env=os.environ):
    legacy = Path(project) / ".qatlib-env/cloud-state.json"
    return legacy if legacy.is_file() else qatlib_home(env) / "state/cloud" / (project_key(project) + ".json")
