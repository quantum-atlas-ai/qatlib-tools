"""Validate the DESIGN.md contract and compare it to its approved digest."""
import argparse
import hashlib
from pathlib import Path
import re

START = "<!-- human-contract:start -->"
END = "<!-- human-contract:end -->"
PATH = "DESIGN.md"
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
    if any(line.strip() in (START, END)
           for line in (root / "README.md").read_text(encoding="utf-8").splitlines()):
        raise ValueError(f"README.md: contract markers belong only in {PATH}")
    approved = None
    for line in (root / MANIFEST).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(rf"([a-f0-9]{{64}})  {re.escape(PATH)}", line)
        if not match or approved is not None:
            raise ValueError(f"{MANIFEST}: invalid or duplicate approval entry")
        approved = match[1]
    protected = block((root / PATH).read_text(encoding="utf-8"), PATH)
    digest = hashlib.sha256(protected.encode("utf-8")).hexdigest()
    return {} if approved == digest else {PATH: digest}


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
