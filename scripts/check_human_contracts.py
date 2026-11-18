"""Validate the two marked contracts and compare them to approved digests."""
import argparse
import hashlib
from pathlib import Path
import re

START = "<!-- human-contract:start -->"
END = "<!-- human-contract:end -->"
PATHS = ("AGENTS.md", "README.md")
MANIFEST = "docs/design/human-contracts.sha256"
MARKER = re.compile(r"<!--\s*human-contract:[^>]*-->")


def block(text: str, path: str) -> str:
    """Return exactly one standalone marked block, with a final LF."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == START]
    ends = [i for i, line in enumerate(lines) if line == END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError(f"{path}: expected one ordered pair of standalone contract markers")
    for line in lines:
        if line not in (START, END) and MARKER.search(re.sub(r"`[^`]*`", "", line)):
            raise ValueError(f"{path}: malformed or inline contract marker")
    return "\n".join(lines[starts[0]:ends[0] + 1]) + "\n"


def pending(root: Path) -> dict[str, str]:
    """Validate the manifest and return the digests awaiting approval."""
    approved = {}
    for line in (root / MANIFEST).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-f0-9]{64})  (AGENTS\.md|README\.md)", line)
        if not match or match[2] in approved:
            raise ValueError(f"{MANIFEST}: invalid or duplicate approval entry")
        approved[match[2]] = match[1]
    candidates = {}
    for path in PATHS:
        protected = block((root / path).read_text(encoding="utf-8"), path)
        digest = hashlib.sha256(protected.encode("utf-8")).hexdigest()
        if approved.get(path) != digest:
            candidates[path] = digest
    return candidates


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", action="store_true", help="report unapproved digests during draft review")
    args = parser.parse_args(argv)
    try:
        changes = pending(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    if changes:
        print("Human review pending for the following contract digests:")
        for path, digest in changes.items():
            print(f"{digest}  {path}")
    return int(bool(changes) and not args.draft)


if __name__ == "__main__":
    raise SystemExit(main())
