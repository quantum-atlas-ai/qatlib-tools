"""Lean source readers shared by planning and retrieval. No compilation/cache management.

Migrated from the original verification/rules.py lexical utilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LEAN_LETTER_LIKE = (r"\u03b1-\u03ba\u03bc-\u03c9\u0391-\u039f\u03a1-\u03a2\u03a4-\u03a9"
                     r"\u03ca-\u03fb\u1f00-\u1ffe\u2100-\u214f\U0001d49c-\U0001d59f"
                     r"\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\u0100-\u017f")

LEAN_ID_FIRST = '[A-Za-z_' + _LEAN_LETTER_LIKE + ']'

LEAN_ID_REST = ("[A-Za-z_0-9'!?" + _LEAN_LETTER_LIKE
                + r"\u2080-\u2089\u2090-\u209c\u1d62-\u1d6a\u2c7c]")

_LITERAL_BOUNDARY = '(?<!' + LEAN_ID_REST[:-1] + '.»∑∏])'

COMMENT_OR_STRING = re.compile(
    r'/-|--|' + _LITERAL_BOUNDARY + r'r#*"|"|«|'
    + _LITERAL_BOUNDARY + r"'(?:[^'\\]|\\(?:[\\\"'rnt]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}))'")

BLOCK_DELIMITER = re.compile(r'/-|-/')

STRING_DELIMITER = re.compile(r'\\[\s\S]|"')

NON_NEWLINES = re.compile(r'[^\n]+')

@dataclass(frozen=True)
class CommentSpan:
    """A top-level comment range in the original source, including delimiters."""
    start: int
    end: int
    kind: str


def mask_comments(text: str, *, preserve_escaped_identifiers: bool = False,
                  comment_spans: list[CommentSpan] | None = None) -> str:
    """Mask Lean comments/literals and escaped identifiers, retaining offsets/newlines."""
    out = []
    i = depth = 0
    quoted = False
    block_start = 0
    while i < len(text):
        # Search in C over ordinary text instead of visiting every proof character
        # in Python. Only comment/string delimiters need state transitions.
        pattern = BLOCK_DELIMITER if depth else STRING_DELIMITER if quoted else COMMENT_OR_STRING
        match = pattern.search(text, i)
        end = match.start() if match else len(text)
        span = text[i:end]
        out.append(NON_NEWLINES.sub(lambda m: ' ' * len(m[0]), span) if depth or quoted else span)
        i = end
        if match is None:
            break
        token = match[0]
        if depth:
            depth += 1 if token == '/-' else -1
            out.append('  ')
            i += 2
            if not depth and comment_spans is not None:
                marker = text[block_start:block_start + 3]
                kind = 'doc' if marker in {'/--', '/-!'} else 'block'
                comment_spans.append(CommentSpan(block_start, i, kind))
        elif quoted:
            if token == '"':
                quoted = False
            # String gaps contain a real newline; keep its original position.
            out.append(NON_NEWLINES.sub(lambda m: ' ' * len(m[0]), token))
            i += len(token)
        elif token == '/-':
            block_start = i
            depth = 1
            out.append('  ')
            i += 2
        elif token == '--':
            j = text.find('\n', i)
            if j == -1:
                j = len(text)
            out.append(' ' * (j - i))
            if comment_spans is not None:
                comment_spans.append(CommentSpan(i, j, 'line'))
            i = j
        elif token.startswith("'"):
            out.append(NON_NEWLINES.sub(lambda m: ' ' * len(m[0]), token))
            i += len(token)
        elif token == '«' or token.startswith('r'):
            # Raw strings close only at a quote with their exact hash prefix;
            # comments, backslashes and shorter delimiters inside are literal.
            closing = '»' if token == '«' else '"' + '#' * (len(token) - 2)
            end = text.find(closing, i + len(token))
            if end < 0:
                raise ValueError('unterminated Lean escaped identifier or raw string')
            end += len(closing)
            masked = NON_NEWLINES.sub(lambda m: ' ' * len(m[0]), text[i:end])
            # Retain guillemets so imports with unsupported escaped module names
            # fail closed instead of disappearing and exposing the next token.
            if token == '«':
                out.append(text[i:end] if preserve_escaped_identifiers else '«' + masked[1:-1] + '»')
            else:
                out.append(masked)
            i = end
        else:
            quoted = True
            out.append(' ')
            i += 1
    if depth or quoted:
        raise ValueError('unterminated Lean block comment or string')
    return ''.join(out)


def comments(text: str) -> list[CommentSpan]:
    """Read comment ranges with the same nested-comment/literal lexer as code."""
    spans: list[CommentSpan] = []
    mask_comments(text, comment_spans=spans)
    return spans

def mask_static_tactic_macros(text: str) -> str:
    """Hide static tactic macros and parameterized term application macros.

    A term macro must return one literal term application quotation, with only
    simple antiquotations of its declared term arguments. Such syntax registers
    term notation, not named public top-level declarations. Arbitrary macro code,
    command quotations and unsupported syntax still fail closed. Offsets and
    newlines are preserved, including inside the hidden quotation.
    """
    masked = mask_comments(text)
    command = re.compile(r'^[ \t]*(?:(?:local|scoped)[ \t]+)?macro\b', re.M)
    header = re.compile(r'[ \t]*(?:local[ \t]+)?macro[ \t]+"[A-Za-z_][A-Za-z0-9_]*"'
                        r'[ \t]*:[ \t]*tactic[ \t]*=>[ \t]*`\(tactic\|')
    term_header = re.compile(
        r'[ \t]*(?:(?:local|scoped)[ \t]+)?macro[ \t]+"[A-Za-z_][A-Za-z0-9_]*[ \t]*"'
        r'(?P<params>(?:[ \t]+[A-Za-z_][A-Za-z0-9_]*:term(?::(?:max|min|[0-9]+))?)+)'
        r'[ \t]*:[ \t]*term[ \t]*=>\s*`\((?:term\|)?')
    identifier = rf'{LEAN_ID_FIRST}{LEAN_ID_REST}*'
    term_atom = re.compile(rf'\s+|\${identifier}|{identifier}(?:\.{identifier})*|[0-9]+|[@()]')
    term_keywords = {'by', 'do', 'let', 'have', 'show', 'from', 'fun', 'match', 'if', 'then',
                     'else', 'where', 'run_tac', 'run_elab', 'run_cmd', 'run_meta',
                     'elab', 'macro', 'syntax', 'theorem', 'lemma', 'def', 'section', 'namespace'}
    spans = []
    covered = 0
    for candidate in command.finditer(masked):
        if candidate.start() < covered:
            continue
        match = header.match(text, candidate.start())
        term = match is None
        if term:
            match = term_header.match(text, candidate.start())
        if match is None:
            raise ValueError('unsupported macro forms need a Lean index')
        parameters = re.findall(r'([A-Za-z_][A-Za-z0-9_]*):term', match.group('params')) if term else []
        if len(parameters) != len(set(parameters)):
            raise ValueError('duplicate term macro parameters')
        # Strings/comments in the raw source must not impersonate this header.
        quote = text.rfind('`(', candidate.start(), match.end())
        if masked[quote:quote + 2] != '`(':
            continue
        expected = [')']
        pairs = {'(': ')', '[': ']', '{': '}'}
        end = match.end()
        while end < len(masked) and expected:
            char = masked[end]
            if char == '$' and term:
                antiquote = re.match(rf'\$({identifier})', masked[end:])
                if antiquote is None or antiquote.group(1) not in parameters:
                    raise ValueError('unsupported term macro antiquotation')
                end += antiquote.end()
                continue
            if char in '`$':
                raise ValueError('nested quotations/antiquotations need a Lean declaration index')
            if char in pairs:
                expected.append(pairs[char])
            elif char in ')]}':
                if char != expected.pop():
                    raise ValueError('unbalanced static tactic macro quotation')
            end += 1
        if expected:
            raise ValueError('unterminated static tactic macro quotation')
        if term:
            # Keep this subset structural: constants, application, parentheses,
            # numerals and declared argument splices. In particular, never mask
            # a `by`/`do` metaprogram or a command quotation as a harmless term.
            body = masked[match.end():end - 1]
            cursor = 0
            while cursor < len(body):
                atom = term_atom.match(body, cursor)
                if atom is None or atom.group() in term_keywords:
                    raise ValueError('unsupported term macro quotation; expected a static application')
                cursor = atom.end()
        newline = masked.find('\n', end)
        line_end = len(masked) if newline < 0 else newline
        if masked[end:line_end].strip():
            raise ValueError('static tactic macro must contain only one quotation')
        # A continuation of the RHS is not a separate source command. Require a
        # recognizable next command (or EOF), rather than guessing its boundary.
        following = masked[line_end:].lstrip()
        if following and not re.match(
                r'(?:@\[|(?:(?:local|scoped)\s+)?macro\b|'
                r'(?:(?:private|protected|noncomputable|partial|unsafe|public|meta)\s+)*'
                r'(?:theorem|lemma|def|abbrev|example|instance|structure|inductive|class)\b|'
                r'(?:namespace|section|end|open|attribute|set_option|variable|noncomputable)\b|#)',
                following):
            raise ValueError('unsupported static tactic macro continuation')
        spans.append((candidate.start(), end))
        covered = end
    result = text
    for start, end in reversed(spans):
        result = result[:start] + NON_NEWLINES.sub(lambda m: ' ' * len(m[0]), result[start:end]) + result[end:]
    return result

def imports(text: str) -> list[str]:
    """Read Lean 4.30's import header, including newlines and import modifiers.

    Module headers have a fixed grammar; do not guess dependencies one line at
    a time. Unsupported module names fail rather than silently escaping a key.
    """
    # Cache by complete content, never path/mtime; repeated validation still reads
    # current bytes. Return a fresh list so callers cannot mutate cached results.
    return list(_import_header(text)[0])

def import_header(text: str) -> tuple[list[str], int]:
    """Return imported modules and the header's end offset in the original text.

    Consumers that stage a Lean file must preserve this prefix verbatim: flattening
    it to module names would discard `module`/`prelude` and public/meta/all imports.
    Comments and literals are masked only for scanning, with offsets retained.
    """
    modules, end = _import_header(text)
    return list(modules), end

def _import_header(text: str) -> tuple[tuple[str, ...], int]:
    masked = mask_comments(text, preserve_escaped_identifiers=True)
    component = rf'(?:«[^«»\n]+»|{LEAN_ID_FIRST}{LEAN_ID_REST}*)'
    module_name = re.compile(rf'{component}(?:\.{component})*')
    # A leading BOM belongs before, never inside, a regenerated driver.
    end = len(masked) - len(masked.lstrip('\ufeff'))
    # The module alternative keeps quoted components containing spaces intact.
    # A boundary prevents accepting the valid prefix of malformed A./A/B names.
    tokens = iter(re.finditer(rf'{module_name.pattern}(?=\s|$)|\S+', masked[end:]))
    offset = end
    current = next(tokens, None)

    def advance() -> str:
        nonlocal current
        current = next(tokens, None)
        return current[0] if current else ''

    token = current[0] if current else ''
    for keyword in ('module', 'prelude'):
        if token == keyword:
            end = offset + current.end()
            token = advance()
    result = []
    while token:
        for modifier in ('public', 'meta'):
            if token == modifier:
                token = advance()
        if token != 'import':
            break
        name = advance()
        if name == 'all':
            name = advance()
        if not module_name.fullmatch(name):
            raise ValueError(f'unsupported import module: {name!r}')
        result.append(name)
        end = offset + current.end()
        token = advance()
    return tuple(result), end
