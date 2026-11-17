"""Incremental, lexical documentation checks; no Lean subprocess or network.

Explicit declaration removal is evidence of a broken reference. An unknown name
is not: generated declarations, private names and namespaces require elaborated
documentation to settle. Source and dependency indexes are separate immutable
layers, with versioned input identities and corruption-checked cache envelopes.
"""
from __future__ import annotations

from collections import Counter
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
from typing import Mapping, Sequence

from qatlib_search import lean_declarations, lean_source

SCHEMA = 1
BIB_ENTRY_RE = re.compile(r"(?m)^\s*@([A-Za-z]+)\s*[({]\s*([^,\s]+)\s*,")
_BIB_START_RE = re.compile(r"(?m)^[ \t]*@([A-Za-z]+)\s*([({])")
REF_LINK_RE = re.compile(r"\[([^\]\n]+)\]\[([A-Za-z][A-Za-z0-9_:-]*)\]")
STANDALONE_RE = re.compile(r"(?<![\w\]`])\[([A-Z][A-Za-z0-9_:-]{2,})(?:,([^\]\n]*))?\](?!\s*\()")
INLINE_RE = re.compile(r"(?<!`)(`+)([^`\n]+)\1(?!`)")
MARKDOWN_PATH_RE = re.compile(r"\]\(([^\s()]+\.lean)(?:#[^\s()]*)?\)")
FENCE_RE = re.compile(r"(?m)^[ \t]*(`{3,}|~{3,})[^\n]*\n")


class SourceLines:
    """Stable line locations without rescanning a source prefix per reference."""
    def __init__(self, text: str):
        self.text = text
        self.starts = [0, *(match.end() for match in re.finditer("\n", text))]

    def line(self, offset: int) -> int:
        return bisect_right(self.starts, offset)

    def context(self, offset: int) -> str:
        index = self.line(offset) - 1
        end = self.starts[index + 1] if index + 1 < len(self.starts) else len(self.text)
        return self.text[self.starts[index]:end].strip()


def blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def compact_blank_lines(text: str) -> str:
    """Drop masked line padding; keep line numbers and all active whitespace."""
    return re.sub(r"(?m)^[ \t]+$", "", text)


def local_lean_path(target: str) -> bool:
    return (target.endswith(".lean") and target != ".lean" and not re.search(r"\s", target)
            and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:|//", target))


def strip_math(text: str) -> str:
    pattern = (r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|"
               r"\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|"
               r"(?<!\\)\$(?!\$)(?:\\.|[^$\n\\])*(?<!\\)\$(?!\$)")
    return re.sub(pattern, lambda match: blank(match[0]), text)


class _SourceExcerpts(HTMLParser):
    """Typed original-source excerpts have their source paper's citation scope."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text: str):
        super().__init__()
        self.token_position = 0
        self.stack: list[tuple[str, int | None]] = []
        self.spans: list[tuple[int, int]] = []
        # Markdown prose may contain unescaped inequalities such as x<epsilon.
        # Feed complete span-tag tokens independently so unrelated prose cannot
        # swallow the next real excerpt tag as an unfinished HTML attribute.
        code_spans = [(match.start(), match.end()) for match in INLINE_RE.finditer(text)]
        code_starts = [start for start, _ in code_spans]
        tokens = re.finditer(r"</?span\b(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>", text, re.I)
        for token in tokens:
            index = bisect_right(code_starts, token.start()) - 1
            in_code = index >= 0 and token.start() < code_spans[index][1]
            if in_code and not any(start is not None for _, start in self.stack):
                continue
            self.token_position = token.start()
            self.feed(token[0])
        self.close()
        if any(start is not None for _, start in self.stack):
            raise ValueError("unterminated typed original-source excerpt")

    def source_offset(self) -> int:
        return self.token_position

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.VOID:
            start = self.source_offset() + len(self.get_starttag_text()) if dict(attrs).get("data-source-excerpt") == "true" else None
            self.stack.append((tag, start))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                closing = self.stack[index:]
                if any(start is not None for _, start in closing[1:]):
                    raise ValueError("unbalanced typed original-source excerpt")
                del self.stack[index:]
                if closing[0][1] is not None:
                    self.spans.append((closing[0][1], self.source_offset()))
                break


def source_excerpt_spans(text: str) -> list[tuple[int, int]]:
    if "data-source-excerpt" not in text:
        return []
    return _SourceExcerpts(text).spans


def strip_source_excerpts(text: str, spans: list[tuple[int, int]] | None = None) -> str:
    spans = source_excerpt_spans(text) if spans is None else spans
    chunks, cursor = [], 0
    for start, end in sorted(spans):
        if end <= cursor:
            continue
        chunks.extend((text[cursor:max(cursor, start)], blank(text[max(cursor, start):end])))
        cursor = end
    return "".join(chunks) + text[cursor:]


def strip_fences(text: str) -> str:
    """Mask Markdown fenced examples, keeping diagnostic offsets stable."""
    result, cursor = [], 0
    while (opening := FENCE_RE.search(text, cursor)) is not None:
        marker = opening[1]
        close = re.search(r"(?m)^[ \t]*" + re.escape(marker[0]) +
                          "{" + str(len(marker)) + r",}[ \t]*(?:\n|$)", text[opening.end():])
        end = opening.end() + close.end() if close else len(text)
        result.extend((text[cursor:opening.start()], blank(text[opening.start():end])))
        cursor = end
    return "".join(result) + text[cursor:]


def canonical_name(name: str) -> str:
    parts = lean_declarations.name_parts(name)
    return ".".join(part if re.fullmatch(lean_declarations.IDENT, part) else f"«{part}»"
                    for part in parts)


@dataclass(frozen=True)
class Reference:
    kind: str
    target: str
    line: int
    context: str = ""

    def identity(self) -> tuple[str, str, str]:
        # A line moving is not a newly written explanation. A changed explanation
        # containing the same internal token is newly written and is checked.
        return self.kind, self.target, self.context


@dataclass
class FileIndex:
    module: str
    declarations: dict[str, str] = field(default_factory=dict)
    namespaces: list[str] = field(default_factory=list)
    generated_roots: list[str] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class LayerIndex:
    identity: dict
    files: dict[str, FileIndex]


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.code}] {self.message}"


def _bib_entry_end(text: str, start: int, opening: str) -> int | None:
    """Find the matching outer delimiter, preserving nested and quoted values."""
    braces = int(opening == "{")
    quoted = escaped = comment = False
    for offset in range(start, len(text)):
        char = text[offset]
        if comment:
            comment = char != "\n"
        elif escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "%" and not quoted and braces == int(opening == "{"):
            comment = True
        elif char == '"':
            if braces == int(opening == "{"):
                quoted = not quoted
        elif char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
            if opening == "{" and not braces and not quoted:
                return offset + 1
        elif char == ")" and opening == "(" and not braces and not quoted:
            return offset + 1
    return None


def parse_bibliography_entries(
    text: str, path: str = "docs/references.bib", *, allow_empty: bool = False
) -> tuple[dict[str, str], list[Diagnostic]]:
    """Read the entry text separately from surrounding commentary."""
    entries: dict[str, str] = {}
    errors: list[Diagnostic] = []
    end = 0
    for header in _BIB_START_RE.finditer(text):
        start = header.start(1) - 1
        if start < end:
            continue
        line = text.count("\n", 0, start) + 1
        end = _bib_entry_end(text, header.end(), header[2])
        if end is None:
            errors.append(Diagnostic("error", "invalid-bib-entry", path, line, "unclosed BibTeX entry"))
            break
        if header[1].lower() in {"comment", "preamble", "string"}:
            continue
        match = BIB_ENTRY_RE.match(text, header.start())
        if match is None or match.end() > end:
            errors.append(Diagnostic("error", "invalid-bib-entry", path, line, "missing BibTeX citation key"))
            continue
        key = match[2]
        if key in entries:
            errors.append(Diagnostic("error", "duplicate-bib-key", path, line, f"duplicate BibTeX key '{key}'"))
        else:
            entries[key] = text[start:end]
    if not entries and not allow_empty and not errors:
        errors.append(Diagnostic("error", "empty-bibliography", path, 1, "no BibTeX entries parsed"))
    return entries, errors


def parse_bibliography(text: str, path: str = "docs/references.bib") -> tuple[set[str], list[Diagnostic]]:
    entries, errors = parse_bibliography_entries(text, path)
    return set(entries), errors


def merge_bibliographies(
    baseline: str, increment: str, *, baseline_path: str, increment_path: str,
    require_thin: bool = False,
) -> tuple[dict[str, str], list[Diagnostic]]:
    """Merge equal keys only when their entry text matches exactly."""
    base, base_errors = parse_bibliography_entries(baseline, baseline_path)
    delta, delta_errors = parse_bibliography_entries(increment, increment_path, allow_empty=True)
    conflicts = [
        Diagnostic("error", "conflicting-bib-key", increment_path, 1,
                   f"BibTeX key '{key}' differs from {baseline_path}")
        for key in sorted(base.keys() & delta.keys()) if base[key] != delta[key]
    ]
    copies = [
        Diagnostic("error", "published-bib-key", increment_path, 1,
                   f"remove BibTeX key '{key}'; it is already present in {baseline_path}")
        for key in sorted(base.keys() & delta.keys()) if require_thin and base[key] == delta[key]
    ]
    entries = base | {key: value for key, value in delta.items() if key not in base}
    return entries, base_errors + delta_errors + conflicts + copies


def citation_references(doc: str, start_line: int = 1) -> list[Reference]:
    cleaned = compact_blank_lines(INLINE_RE.sub(lambda m: blank(m[0]), strip_math(strip_fences(doc))))
    locations = SourceLines(cleaned)
    found = []
    for match in REF_LINK_RE.finditer(cleaned):
        found.append(Reference("citation", match[2], start_line + locations.line(match.start()) - 1))
    cleaned = REF_LINK_RE.sub(lambda m: blank(m[0]), cleaned)
    for match in STANDALONE_RE.finditer(cleaned):
        # Ordinary mathematical brackets such as [ABC] and [Nat] are not a
        # citation convention. CamelCase, a year, or a separator identifies keys.
        key = match[1]
        if not (re.search(r"[0-9]{2,}[a-z]?$", key) or
                (re.search(r"[a-z]", key) and re.search(r"[A-Z].*[A-Z]", key))):
            continue
        line = start_line + locations.line(match.start()) - 1
        found.append(Reference("citation", key, line))
        if match[2] is not None:
            found.append(Reference("citation-locator", key, line))
    return found


def analyze_document(text: str, path: str, *, internal_patterns: Sequence[str] = ()) -> FileIndex:
    """Index reader Markdown with the same citation/name/path conventions."""
    result = FileIndex("")
    cleaned = compact_blank_lines(strip_fences(text))
    locations = SourceLines(cleaned)
    original_locations = SourceLines(text)
    excerpts = source_excerpt_spans(cleaned)
    result.references.extend(citation_references(strip_source_excerpts(cleaned, excerpts)))

    def add(kind: str, target: str, offset: int, *, context: bool = False) -> None:
        line = locations.line(offset)
        result.references.append(Reference(kind, target, line,
                                           original_locations.context(original_locations.starts[line - 1]) if context else ""))

    boundaries = sorted({0, len(cleaned), *(position for span in excerpts for position in span)})
    for start, end in zip(boundaries, boundaries[1:]):
        # Paper excerpts can contain unmatched source backticks. Reset inline
        # code scope at that typed boundary, preserving API refs in other cells.
        for match in INLINE_RE.finditer(cleaned, start, end):
            target = match[2].strip()
            if local_lean_path(target):
                add("path", target, match.start())
            elif lean_declarations.NAME_RE.fullmatch(target):
                name = canonical_name(target)
                if "." in name and name.split(".")[0][:1].isupper():
                    add("name", name, match.start())
    for match in MARKDOWN_PATH_RE.finditer(cleaned):
        if local_lean_path(match[1]):
            add("path", match[1], match.start())
    for pattern in internal_patterns:
        for match in re.finditer(pattern, cleaned):
            add("internal", match[0], match.start(), context=True)
    return result


def analyze_source(text: str, path: str, *, internal_patterns: Sequence[str] = (),
                   documentation: bool = True) -> FileIndex:
    result = FileIndex(path.removesuffix(".lean").replace("/", "."))
    spans: list[lean_source.CommentSpan] = []
    masked = (lean_source.mask_comments(text, preserve_escaped_identifiers=True, comment_spans=spans)
              if documentation else "")
    try:
        declarations = lean_declarations.declaration_kinds(text)
    except ValueError as error:
        result.problems.append(str(error))
        declarations = {}
    for parts, kind in declarations.items():
        name = canonical_name(".".join(part if re.fullmatch(lean_declarations.IDENT, part)
                                        else f"«{part}»" for part in parts))
        result.declarations[name] = kind
        for end in range(1, len(parts)):
            result.namespaces.append(".".join(parts[:end]))
        if kind in {"structure", "inductive", "class"}:
            result.generated_roots.append(name)
    result.namespaces = sorted(set(result.namespaces))
    if not documentation:
        return result
    for match in re.finditer(r"(?m)^\s*namespace\s+(" + lean_declarations.NAME + ")", masked):
        result.namespaces.append(canonical_name(match[1]))
    result.namespaces = sorted(set(result.namespaces))
    # c-prefixed mathematical variables in binders are not workflow identifiers.
    local_names = set()
    for match in re.finditer(r"[({\[]\s*([^:\n(){}\[\]]{1,200}):", masked):
        names = match[1].split()
        if all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", name) for name in names):
            local_names.update(names)
    patterns = [re.compile(pattern) for pattern in internal_patterns]
    locations = SourceLines(text)

    def add(kind: str, target: str, offset: int, *, context: bool = False) -> None:
        line = locations.line(offset)
        result.references.append(Reference(kind, target, line, locations.context(offset) if context else ""))

    for span in spans:
        # Lean's /-- error: ... -/ before #guard_msgs is expected output, not
        # reader documentation. The same lexer has already excluded strings.
        if re.match(r"\s*#guard_msgs\b", masked[span.end:]):
            continue
        doc = strip_fences(text[span.start:span.end])
        if span.kind == "doc":
            line = locations.line(span.start)
            result.references.extend(citation_references(doc, line))
            for match in INLINE_RE.finditer(doc):
                target = match[2].strip()
                if local_lean_path(target):
                    add("path", target, span.start + match.start())
                elif lean_declarations.NAME_RE.fullmatch(target):
                    name = canonical_name(target)
                    if "." in name and name.split(".")[0][:1].isupper():
                        add("name", name, span.start + match.start())
            for match in MARKDOWN_PATH_RE.finditer(doc):
                if local_lean_path(match[1]):
                    add("path", match[1], span.start + match.start())
        for pattern in patterns:
            for match in pattern.finditer(doc):
                target = match[0]
                if target in local_names and not re.search(r"(?i)\bclaim\b", doc):
                    continue
                add("internal", target, span.start + match.start(), context=True)
    # Only declaration names are subject to identifier policy; local variables,
    # proof terms and string-valued diagnostics must never be scanned as prose.
    try:
        all_names = lean_declarations.declaration_kinds(text, include_private=True)
    except ValueError:
        all_names = {}
    for parts in all_names:
        name = ".".join(parts)
        for pattern in patterns:
            for match in pattern.finditer(name):
                result.references.append(Reference("internal-name", match[0], 1, name))
    return result


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def parser_identity() -> str:
    return digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (Path(__file__), Path(lean_source.__file__), Path(lean_declarations.__file__))})


def input_identity(sources: Mapping[str, str], *, toolchain: str, manifest: str,
                   tree: str, internal_patterns: Sequence[str] = ()) -> dict:
    return {"schema": SCHEMA, "parser": parser_identity(), "toolchain": toolchain,
            "manifest": hashlib.sha256(manifest.encode()).hexdigest(), "tree": tree,
            "sources": digest(sources), "policy": list(internal_patterns)}


def build_layer(sources: Mapping[str, str], identity: dict,
                *, internal_patterns: Sequence[str] = (), documentation: bool = True) -> LayerIndex:
    return LayerIndex(identity, {path: (analyze_document(text, path, internal_patterns=internal_patterns)
                                       if path.endswith(".md") else
                                       analyze_source(text, path, internal_patterns=internal_patterns,
                                                      documentation=documentation))
                                for path, text in sorted(sources.items())})


def write_cache(path: Path, layer: LayerIndex) -> None:
    payload = asdict(layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"payload": payload, "checksum": digest(payload)}, ensure_ascii=False), encoding="utf-8")


def read_cache(path: Path, expected_identity: dict) -> LayerIndex | None:
    """Reject stale/corrupt caches; callers rebuild from the authoritative tree."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        payload = envelope["payload"]
        if envelope["checksum"] != digest(payload) or payload["identity"] != expected_identity:
            return None
        files = {}
        for name, data in payload["files"].items():
            files[name] = FileIndex(**{**data, "references": [Reference(**r) for r in data["references"]]})
        return LayerIndex(payload["identity"], files)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cached_layer(sources: Mapping[str, str], identity: dict, cache_directory: Path,
                  *, internal_patterns: Sequence[str] = (), documentation: bool = True,
                  prefix: str = "source", statistics: dict | None = None) -> LayerIndex:
    """Assemble a tree-bound layer from individually content-verified file records.

    Per-file lexical records do not depend on Git HEAD or dependency pins. The
    assembled layer does: its exact full input identity is still verified on
    every read. A source edit reparses one file rather than all reader pages.
    """
    stats = statistics if statistics is not None else {}
    stats.update(layer_hits=0, file_hits=0, files_parsed=0)
    layer_path = cache_directory / (prefix + "-" + digest(identity) + ".json")
    cached = read_cache(layer_path, identity)
    if cached is not None:
        stats["layer_hits"] = 1
        return cached
    files = {}
    for path, text in sorted(sources.items()):
        mode = "markdown" if path.endswith(".md") else "lean-doc" if documentation else "lean-declarations"
        record_identity = {"schema": SCHEMA, "parser": identity["parser"], "path": path,
                           "content": hashlib.sha256(text.encode()).hexdigest(),
                           "policy": list(internal_patterns), "mode": mode}
        record_path = cache_directory / "files" / (digest(record_identity) + ".json")
        record = read_cache(record_path, record_identity)
        if record is None or set(record.files) != {path}:
            record = build_layer({path: text}, record_identity,
                                 internal_patterns=internal_patterns, documentation=documentation)
            write_cache(record_path, record)
            stats["files_parsed"] += 1
        else:
            stats["file_hits"] += 1
        files[path] = record.files[path]
    result = LayerIndex(identity, files)
    write_cache(layer_path, result)
    return result


def check_changes(before: LayerIndex, after: LayerIndex, *, bib_keys: set[str],
                  before_bib_keys: set[str] | None = None,
                  dependencies_before: Sequence[LayerIndex] = (), dependencies_after: Sequence[LayerIndex] = (),
                  retired_roots: Sequence[str] = (), existing_paths: set[str] | None = None) -> list[Diagnostic]:
    """Inspect added refs and all unchanged consumers affected by provider removal."""
    old_layers, new_layers = [before, *dependencies_before], [after, *dependencies_after]
    incomplete = []
    seen_problems = set()
    for layer in [*old_layers, *new_layers]:
        dependency = layer is not before and layer is not after
        for path, file in layer.files.items():
            for problem in file.problems:
                identity = (path, problem, dependency)
                if identity not in seen_problems:
                    seen_problems.add(identity)
                    incomplete.append(Diagnostic(
                        "error", "required-index-incomplete" if dependency else "source-index-incomplete",
                        path, 1, ("locked dependency index incomplete: " if dependency else "") + problem))
    # No deletion claim is justified by an incomplete required layer, including
    # the previous locked dependency tree during an upgrade.
    if incomplete:
        return incomplete

    def union(attribute: str, layers: Sequence[LayerIndex]) -> set[str]:
        return {name for layer in layers for file in layer.files.values() for name in getattr(file, attribute)}

    declarations = union("declarations", new_layers)
    removed = union("declarations", old_layers) - declarations
    modules = {file.module for layer in new_layers for file in layer.files.values() if file.module}
    removed_modules = {file.module for layer in old_layers for file in layer.files.values() if file.module} - modules
    namespaces = union("namespaces", new_layers)
    known_names = declarations | modules | namespaces
    generated = union("generated_roots", new_layers)
    removed_keys = (before_bib_keys or set()) - bib_keys
    paths = existing_paths if existing_paths is not None else {path for layer in new_layers for path in layer.files}
    old_paths = {path for layer in old_layers for path in layer.files}
    deleted_paths = old_paths - paths
    diagnostics: list[Diagnostic] = []
    for path, file in after.files.items():
        old_file = before.files.get(path)
        previous = Counter(ref.identity() for ref in old_file.references) if old_file else Counter()
        for ref in file.references:
            identity = ref.identity()
            added = previous[identity] == 0
            if previous[identity]:
                previous[identity] -= 1
            error = warning = code = ""
            if ref.kind == "citation" and (added or ref.target in removed_keys):
                if ref.target not in bib_keys:
                    error, code = f"unknown citation key [{ref.target}] (not in references.bib)", "undefined-citation"
            elif ref.kind == "citation-locator" and added:
                error, code = f"write [{ref.target}], Theorem/Lemma/Equation locator outside the brackets", "citation-locator"
            elif ref.kind in {"internal", "internal-name"} and added:
                error, code = f"internal workflow identifier {ref.target!r} in source or reader documentation", "internal-identifier"
            elif ref.kind == "name" and (added or ref.target in removed or ref.target in removed_modules):
                if any(ref.target == root or ref.target.startswith(root + ".") for root in retired_roots):
                    error, code = f"retired documentation root `{ref.target}`", "retired-root"
                elif ref.target in removed:
                    if any(ref.target.startswith(root + ".") for root in generated):
                        warning, code = f"`{ref.target}` may now be generated; verify its exported HTML target", "generated-name-review"
                    else:
                        error, code = f"reference to deleted explicit declaration `{ref.target}`", "deleted-declaration"
                elif ref.target in removed_modules and ref.target not in namespaces:
                    error, code = f"reference to deleted module `{ref.target}`", "deleted-module"
                elif ref.target not in known_names:
                    warning, code = f"unknown lexical name `{ref.target}`; verify in elaborated API documentation", "unknown-name"
            elif ref.kind == "path":
                candidates = {posixpath.normpath(ref.target), posixpath.normpath(posixpath.join(posixpath.dirname(path), ref.target))}
                if (added or candidates & deleted_paths) and not candidates & paths:
                    error, code = f"missing Lean source path `{ref.target}`", "missing-lean-path"
            if error or warning:
                diagnostics.append(Diagnostic("error" if error else "warning", code, path, ref.line, error or warning))
    return diagnostics
