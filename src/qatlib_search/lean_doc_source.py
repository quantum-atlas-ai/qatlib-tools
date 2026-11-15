"""Conservative Lean declaration/docstring reader used by v2 docs and retrieval.

This extracts explicit public declarations and source headers, not elaborated types.
It writes no documentation; gen_v2_docs owns the generated usage documentation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from qatlib_search.paths import ROOT
from qatlib_search.lean_source import LEAN_ID_REST, mask_comments, mask_static_tactic_macros

DECL_PREFIX = (
    r"^(?P<attrs>(?:@\[[^\]]*\]\s*)*)"
    r"(?P<mods>(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local|public|meta)\s+)*)"
)
DECL_RE = re.compile(
    DECL_PREFIX +
    r"(?P<kind>def|abbrev|theorem|lemma|structure|inductive|class|opaque|axiom|instance)\s+"
    r"(?P<name>[^\s:({\[]+?)(?=\.\{|[\s:({\[]|$)"
)
EXAMPLE_RE = re.compile(DECL_PREFIX + r"(?P<kind>example)(?=[\s:({\[]|$)")
ATTR_ONLY_RE = re.compile(r"^\s*@\[[^\]]*\]\s*$")
NAMESPACE_RE = re.compile(r"^namespace\s+(\S+)\s*$")
SECTION_RE = re.compile(
    r"^(?:@\[expose\]\s*)?(?P<mods>(?:(?:public|noncomputable|meta)\s+)*)"
    r"section(?:\s+(?P<name>\S+))?\s*$")
END_RE = re.compile(r"^end(?:\s+(\S+))?\s*$")
VARIABLE_RE = re.compile(r"^variable(?:\s|$)")

DEFINITION_KINDS = {"def", "abbrev", "structure", "inductive", "class", "opaque"}
THEOREM_KINDS = {"theorem", "lemma"}
STRUCTURED_KINDS = {"structure", "inductive", "class"}


@dataclass
class Decl:
    module: str
    fqn: str
    kind: str
    signature: str  # source header fragment, not an elaborated declaration type
    doc: str
    line: int
    variable_context: tuple[str, ...] = ()


@dataclass
class Module:
    name: str
    path: Path
    header: str = ""
    decls: list[Decl] = field(default_factory=list)


def module_name(path: Path, root: Path | None = None) -> str:
    return ".".join(path.relative_to(ROOT if root is None else root).with_suffix("").parts)


def first_sentence(doc: str) -> str:
    """First sentence of a docstring (bold markers dropped).  Periods inside backticks,
    abbreviations (`i.e.`, `e.g.`, `cf.`) and a very short lead-in such as `Born rule.` do
    not end the summary."""
    text = " ".join(doc.split())
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # drop bold markers
    depth = 0
    for i, ch in enumerate(text):
        if ch == "`":
            depth ^= 1
        elif ch == "." and depth == 0 and (i + 1 == len(text) or text[i + 1] == " "):
            head = text[: i + 1]
            if re.search(r"\b(i\.e|e\.g|cf|resp|vs)\.$", head):
                continue
            if len(head) < 24 and i + 1 < len(text):
                continue  # too short to be informative; include the next sentence
            return head
    return text


def module_header(lines: list[str]) -> str:
    """The module docstring `/-! ... -/`, first paragraph after the `#` title."""
    text = "\n".join(lines)
    m = re.search(r"/-!(.*?)-/", text, re.S)
    if not m:
        return ""
    body = m.group(1).strip().splitlines()
    body = [ln for ln in body if not ln.startswith("#")]
    para: list[str] = []
    for ln in body:
        if not ln.strip():
            if para:
                break
            continue
        para.append(ln.strip())
    return " ".join(para)


def split_signature(text: str, kind: str = "", masked: str | None = None) -> str:
    """Written header before assignment, fields/constructors, or equation alternatives.

    A line-leading bar can start an absolute value in the result type. For
    equation-style bodies require a top-level => before the assignment boundary;
    constructor bars are recognized separately from the declaration kind.
    This remains lexical: a header combining a line-leading absolute value with
    an equation-style body requires reading the source for the complete type.
    """
    if masked is None:
        masked = mask_comments(text, preserve_escaped_identifiers=True)
    depth = 0
    line_start = 0
    alternative_start: int | None = None
    i = 0
    while i < len(text):
        ch = masked[i]
        if ch == "\n":
            line_start = i + 1
        if ch in "([{⟨":
            depth += 1
        elif ch in ")]}⟩":
            depth -= 1
        elif depth == 0:
            if masked.startswith(":=", i):
                return text[:i]
            if masked.startswith(" where", i) or masked.startswith("\nwhere", i):
                return text[:i]
            if ch == "|":
                if alternative_start is None and line_start and not masked[line_start:i].strip():
                    if kind in STRUCTURED_KINDS:
                        return text[:line_start - 1]
                    alternative_start = line_start - 1
            if alternative_start is not None and masked.startswith("=>", i):
                return text[:alternative_start]
        i += 1
    return text


def strip_line_comment(line: str) -> str:
    """Drop a trailing -- comment without cutting a quoted string literal."""
    quoted = escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
        elif quoted and ch == "\\":
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif not quoted and line.startswith("--", i):
            return line[:i]
    return line


def variable_binders_complete(text: str) -> bool:
    """Check only binder delimiters, not their Lean contents; reject command-local `in`."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    expected: list[str] = []
    quoted = escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
        elif quoted:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif not expected and not ch.isspace() and ch not in pairs:
            raise ValueError("only variable binder groups are supported; `variable ... in` is unsupported")
        elif ch == '"':
            quoted = True
        elif text.startswith("/-", i):
            raise ValueError("block comments inside variable binders are unsupported")
        elif ch in pairs:
            expected.append(pairs[ch])
        elif ch in ")]}":
            if not expected or expected.pop() != ch:
                raise ValueError("unbalanced variable binder delimiters")
    return not expected and not quoted


def read_variable(lines: list[str], start: int) -> tuple[str, int]:
    """Collect one variable command and indented binder continuations."""
    bindings = strip_line_comment(lines[start]).lstrip()[len("variable") :].strip()
    complete = variable_binders_complete(bindings)
    end = start
    while end + 1 < len(lines):
        nxt = strip_line_comment(lines[end + 1]).rstrip()
        if not nxt.strip():
            end += 1
            continue
        if not nxt[0].isspace() or (complete and nxt.lstrip()[0] not in "([{"):
            break
        end += 1
        bindings += "\n" + nxt.strip()
        complete = variable_binders_complete(bindings)
    if not bindings or not complete:
        raise ValueError("variable binders must be complete, with continuations on indented lines")
    return "variable " + " ".join(bindings.split()), end


def read_comment(lines: list[str], start: int) -> tuple[str, int]:
    """Read a standalone (possibly nested) block comment, retaining docstring text."""
    depth = 0
    chunk: list[str] = []
    for end in range(start, len(lines)):
        line = lines[end]
        for match in re.finditer(r"/-|-/", line):
            depth += 1 if match.group() == "/-" else -1
            if depth == 0:
                if strip_line_comment(line[match.end() :]).strip():
                    raise ValueError("block comments and declarations must be on separate lines")
                chunk.append(line[: match.end()])
                return "\n".join(chunk).strip(), end
        chunk.append(line)
    raise ValueError("unterminated block comment")


def parse_module(path: Path, root: Path | None = None) -> Module:
    return parse_module_text(path.read_text(encoding="utf-8"), path, root)


def body_layout(text: str, masked: str, header: str, column: int) -> tuple[bool, int | None]:
    """Find a declaration body's initial layout, without elaborating its terms.

    A declaration may itself be indented. Its body still owns more-indented
    commands until a dedent; this keeps local helpers out of the public index.
    An inline complete term does not force the next command to column zero.
    """
    tail = masked[len(header):]
    if not tail.startswith(":="):
        return bool(tail.strip()), None
    written = text[len(header) + 2:].strip()
    tail = tail[2:]
    layouts = list(re.finditer(r"\b(?:by|do)\b", tail))
    if layouts:
        layout_end = layouts[-1].end()
        rest = tail[layout_end:].lstrip(" \t")
        if not rest:
            return True, None
        offset = len(header) + 2 + len(tail) - len(rest)
        newline = masked.rfind("\n", 0, offset)
        return True, offset - newline - 1 if newline >= 0 else column + offset
    incomplete = any(tail.count(a) > tail.count(b) for a, b in zip("([{⟨", ")]}⟩"))
    incomplete = incomplete or bool(re.search(r"(?:=>|\b(?:then|else|with|where))\s*$", tail))
    return not written or incomplete, None


def parse_module_text(text: str, path: Path, root: Path | None = None) -> Module:
    """Apply the same conservative lexer to catalog/archive text without a checkout."""
    original_masked_lines = mask_comments(text, preserve_escaped_identifiers=True).split("\n")
    text = mask_static_tactic_macros(text)
    masked = mask_comments(text, preserve_escaped_identifiers=True)
    module_mode = bool(re.match(rf"\s*module(?!{LEAN_ID_REST})", masked.lstrip("\ufeff")))
    if re.search(
            r"`\s*\(|^\s*(?:run_cmd|run_meta|run_elab|elab|macro|syntax)\b", masked, re.MULTILINE):
        raise ValueError(f"{path}: generated declarations need a Lean index")
    # The mask preserves LF offsets; splitlines() also treats characters inside
    # Lean strings (for example U+2028) as line separators and would misalign it.
    lines = text.split("\n")
    masked_lines = masked.split("\n")
    mod = Module(name=module_name(path, root), path=path, header=module_header(lines))
    # Each scope saves the context length from before it opened. Namespace variables
    # are local too: reopening the same namespace must not restore old variables.
    stack: list[tuple[str, str, int, bool]] = []  # kind, written name, context length, public default
    is_public = not module_mode
    variable_context: list[str] = []
    pending_doc: str | None = None
    declaration_indent: int | None = None
    body_indent: int | None = None
    body_pending = False

    def at_command_level(indent: int) -> bool:
        return (declaration_indent is None or indent <= declaration_indent
                or (body_indent is not None and indent < body_indent)
                or (body_indent is None and not body_pending))

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        if stripped.startswith("/-"):
            comment, end = read_comment(lines, i)
            if comment.startswith("/--") and at_command_level(indent):
                pending_doc = comment[3:-2].strip()
            elif not at_command_level(indent):
                pending_doc = None
            i = end + 1
            continue
        line = strip_line_comment(line).rstrip()
        command = masked_lines[i].rstrip().lstrip(" \t")
        offset = len(masked_lines[i].rstrip()) - len(command)
        if not command.strip():
            if original_masked_lines[i].strip():
                pending_doc = None  # A hidden macro consumes its own docstring.
            i += 1
            continue
        if declaration_indent is not None and re.match(r"where(?:\s|$)", command):
            body_pending, body_indent = True, None
            pending_doc = None
            i += 1
            continue
        if not at_command_level(indent):
            body_indent = indent if body_indent is None else min(body_indent, indent)
            pending_doc = None
            i += 1
            continue
        declaration_indent, body_indent, body_pending = None, None, False
        if ATTR_ONLY_RE.match(command):
            i += 1
            continue
        if re.fullmatch(r"(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|partial|"
                        r"unsafe|scoped|local|public|meta)\s*)+", command):
            raise ValueError(f"{path}:{i + 1}: standalone declaration modifiers are unsupported")
        m = NAMESPACE_RE.match(command)
        if m:
            if "«" in m.group(1) or "»" in m.group(1):
                raise ValueError(f"{path}:{i + 1}: quoted namespace identifiers are unsupported")
            for component in m.group(1).split("."):
                stack.append(("namespace", component, len(variable_context), is_public))
            pending_doc = None
            i += 1
            continue
        m = SECTION_RE.match(command)
        if m:
            for component in (m.group("name") or "").split("."):
                stack.append(("section", component, len(variable_context), is_public))
                is_public = is_public or "public" in m.group("mods").split()
            pending_doc = None
            i += 1
            continue
        m = END_RE.match(command)
        if m:
            ending = m.group(1).split(".") if m.group(1) is not None else None
            count = len(ending) if ending is not None else 1
            if len(stack) < count or (ending is not None and
                    ending != [name for _, name, _, _ in stack[-count:]]):
                raise ValueError(f"{path}:{i + 1}: end does not match the innermost namespace/section")
            for _ in range(count):
                _, _, context_length, is_public = stack.pop()
                del variable_context[context_length:]
            pending_doc = None
            i += 1
            continue
        if VARIABLE_RE.match(command):
            try:
                command, end = read_variable(lines, i)
            except ValueError as error:
                raise ValueError(f"{path}:{i + 1}: {error}") from error
            variable_context.append(command)
            pending_doc = None
            i = end + 1
            continue
        m = DECL_RE.match(command) or EXAMPLE_RE.match(command)
        if m:
            kind = m.group("kind")
            name = m.group("name") if kind != "example" else ""
            if "«" in name or "»" in name:
                raise ValueError(f"{path}:{i + 1}: quoted declaration identifiers are unsupported")
            modifiers = m.group("mods").split()
            declaration_public = "private" not in modifiers and (is_public or "public" in modifiers)
            # accumulate the declaration text until the body starts
            j = i
            start = offset + m.end()
            chunk = [line[start:]]
            # Slice the whole-module mask: incremental chunks may end inside a
            # multiline string, which cannot be masked independently.
            masked_chunk = [masked_lines[i][start: len(line)]]
            body = split_signature("\n".join(chunk), kind, "\n".join(masked_chunk))
            while body == "\n".join(chunk) and j + 1 < len(lines):
                nxt = strip_line_comment(lines[j + 1]).rstrip()
                next_command = masked_lines[j + 1].strip()
                next_indent = len(nxt) - len(nxt.lstrip(" \t"))
                if (DECL_RE.match(next_command) or EXAMPLE_RE.match(next_command)
                        or NAMESPACE_RE.match(next_command)
                        or SECTION_RE.match(next_command) or END_RE.match(next_command)
                        or VARIABLE_RE.match(next_command)):
                    break
                if nxt and next_indent <= indent and not nxt.lstrip().startswith("|"):
                    break
                j += 1
                chunk.append(nxt)
                masked_chunk.append(masked_lines[j][:len(nxt)])
                body = split_signature("\n".join(chunk), kind, "\n".join(masked_chunk))
            signature = " ".join(body.split())
            declaration_indent = indent
            body_pending, body_indent = body_layout(
                "\n".join(chunk), "\n".join(masked_chunk), body, start)
            if kind not in {"instance", "example"} and declaration_public:
                ns = ""
                for kind_, name_, _, _ in stack:
                    if kind_ == "namespace":
                        ns = (name_.removeprefix("_root_.") if name_.startswith("_root_.")
                              else f"{ns}.{name_}" if ns else name_)
                fqn = f"{ns}.{name}" if ns and not name.startswith("_root_.") else name
                mod.decls.append(
                    Decl(
                        module=mod.name,
                        fqn=fqn.removeprefix("_root_."),
                        kind=kind,
                        signature=signature,
                        doc=(pending_doc or "").strip(),
                        line=i + 1,
                        variable_context=tuple(variable_context),
                    )
                )
            pending_doc = None
            i = j + 1
            continue
        # Unindexed code consumes its doc too (e.g. an indented structure field
        # or local helper); it must not document the next public declaration.
        pending_doc = None
        i += 1
    return mod
