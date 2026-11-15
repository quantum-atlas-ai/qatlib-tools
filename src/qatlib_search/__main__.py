from __future__ import annotations

import argparse
import json

from .index import SearchIndex


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qatlib-search", description="index and query Lean declarations")
    sub = p.add_subparsers(dest="command")
    build = sub.add_parser("build"); build.add_argument("roots", nargs="+"); build.add_argument("-o", "--output", required=True)
    query = sub.add_parser("query"); query.add_argument("index"); query.add_argument("term"); query.add_argument("--kind"); query.add_argument("--limit", type=int, default=20)
    retrieve = sub.add_parser("retrieve", help="run configured unified retrieval")
    retrieve.add_argument("args", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    if args.command == "build":
        index = SearchIndex(args.roots); index.build(); index.save(args.output); return 0
    if args.command == "query": print(json.dumps([x.__dict__ for x in SearchIndex.load(args.index).query(args.term, kind=args.kind, limit=args.limit)], ensure_ascii=False, indent=2)); return 0
    if args.command == "retrieve":
        from .retrieve import main as retrieve_main
        return retrieve_main(args.args)
    p.print_help(); return 0


if __name__ == "__main__":
    raise SystemExit(main())

