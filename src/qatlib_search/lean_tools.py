"""Source retrieval and managed Lean environment commands."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["retrieve"]:
        from .lookup import main as lookup
        return lookup(args[1:])
    if args[:1] == ["env"]:
        from qatlib_env.__main__ import main as environment
        env_args = args[1:]
        if not env_args or env_args[0].startswith("-"):
            env_args.insert(0, "env")
        return environment(env_args)
    parser = argparse.ArgumentParser(prog="qatlib-lean-tools", description=__doc__)
    from qatlib_env import __version__
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("retrieve", help="look up Lean declarations by source name or module")
    commands.add_parser("env", help="run qatlib-env commands")
    parser.parse_args(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
