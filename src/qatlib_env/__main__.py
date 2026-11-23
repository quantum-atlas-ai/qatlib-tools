"""Aggregate command line entry point for the shared environment package."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        from qatlib_env import __version__
        print(f"qatlib-env {__version__}")
        return 0
    if args[:1] == ["exec"]:
        from qatlib_env.execute import main as execute
        return execute(args[1:])
    if args[:1] == ["workspace"]:
        from qatlib_env.inventory import cli_main
        return cli_main(args[1:])
    if args[:1] == ["ci"]:
        from qatlib_env.ci import cli_main
        return cli_main(args[1:])
    index = 2 if args[:1] == ["--project"] else 0
    if args[index:index + 1] in (["server"], ["server-check"]):
        from qatlib_env.server import main as server_main
        action = ["check"] if args[index] == "server-check" else []
        return server_main([*args[:index], *action, *args[index + 1:]])
    from qatlib_env.workspace import cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
