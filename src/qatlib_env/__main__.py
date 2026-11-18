"""Aggregate command line entry point for the shared environment package."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["exec"]:
        from qatlib_env.execute import main as execute
        return execute(args[1:])
    if args[:1] == ["workspace"]:
        from qatlib_env.inventory import cli_main
        return cli_main(args[1:])
    if args[:1] == ["ci"]:
        from qatlib_env.ci import cli_main
        return cli_main(args[1:])
    from qatlib_env.workspace import cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
