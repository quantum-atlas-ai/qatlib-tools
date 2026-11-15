from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class Declaration:
    name: str
    kind: str
    path: str
    line: int
    text: str


def parse_imports(text: str) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        match = re.match(r"\s*import\s+(.+?)\s*(?:--.*)?$", line)
        if not match: continue
        body = match.group(1).strip()
        if body.startswith(("\"", "«")) and body.endswith(("\"", "»")):
            body = body[1:-1]
        result.extend(x for x in re.split(r"[\s,]+", body) if x)
    return result


class SearchIndex:
    def __init__(self, roots: list[str | Path] | tuple[str | Path, ...]):
        self.roots = tuple(Path(x) for x in roots); self.records: list[Declaration] = []

    def build(self) -> list[Declaration]:
        records: list[Declaration] = []
        pattern = re.compile(r"^\s*(theorem|lemma|def|abbrev|structure|inductive|class)\s+([A-Za-z_][\w'.]*)")
        for root in self.roots:
            if root.is_file(): paths = [root]
            else: paths = sorted(root.rglob("*.lean"))
            for path in paths:
                try: lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError): continue
                for number, line in enumerate(lines, 1):
                    match = pattern.match(line)
                    if match: records.append(Declaration(match[2], match[1], str(path), number, line.strip()))
        self.records = records; return records

    def query(self, term: str, *, kind: str | None = None, limit: int = 50) -> list[Declaration]:
        term = term.casefold()
        return [record for record in self.records if term in record.name.casefold() and (kind is None or record.kind == kind)][:limit]

    def save(self, path: str | Path) -> Path:
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(x) for x in self.records], ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SearchIndex":
        obj = cls([]); obj.records = [Declaration(**row) for row in json.loads(Path(path).read_text(encoding="utf-8"))]; return obj

