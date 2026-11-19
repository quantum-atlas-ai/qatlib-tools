"""Resolve renamed settings without changing the caller's environment."""

_UNSET = object()


def legacy_name(name):
    return "QATLAS_" + name.removeprefix("QATLIB_")


def env_value(env, name, default=_UNSET):
    """Read the canonical key first, including an explicitly empty value."""
    if name in env:
        return env[name]
    old_name = legacy_name(name)
    if old_name in env:
        return env[old_name]
    if default is _UNSET:
        raise KeyError(name)
    return default
