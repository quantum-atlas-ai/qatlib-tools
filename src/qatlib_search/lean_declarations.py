from __future__ import annotations

import re

from qatlib_search.lean_source import LEAN_ID_FIRST, LEAN_ID_REST, mask_comments, mask_static_tactic_macros

IDENT = rf"(?:«[^«»\n]+»|{LEAN_ID_FIRST}{LEAN_ID_REST}*)"
NAME = rf"{IDENT}(?:\.{IDENT})*"
NAME_RE = re.compile(NAME)
IDENT_RE = re.compile(IDENT)
ATTRIBUTE_RE = re.compile(r"@\[[^\]]*\]")
COMMAND_RE = re.compile(
    r"^[ \t]*(?P<mods>(?:@\[[^\]]*\]\s*|"
    r"(?:private|protected|noncomputable|partial|unsafe|public|meta)\s+)*)"
    rf"(?:(?P<scope>namespace|section|end)(?!{LEAN_ID_REST})(?P<body>[^\n]*)|"
    r"(?P<kind>theorem|lemma|def|abbrev|instance|opaque|structure|inductive|class)\s+(?P<header>[^\n]*))",
    re.MULTILINE)


def name_parts(name: str) -> tuple[str, ...]:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"unsupported Lean name {name!r}")
    return tuple(part[1:-1] if part.startswith("«") else part
                 for part in IDENT_RE.findall(name))


def declaration_kinds(text: str, *, include_private: bool = False) -> dict[tuple[str, ...], str]:
    """Index explicit public declarations, optionally private headers for candidate selection.

    Private header names are not addressable public FQNs. The opt-in view is for
    locating whole source files, never duplicate-public-declaration comparison.
    """
    masked = mask_comments(mask_static_tactic_macros(text), preserve_escaped_identifiers=True)
    prefix = "__qatlas_quoted_"
    while prefix in masked:
        prefix += "_"
    quoted: dict[str, str] = {}

    def protect(match: re.Match) -> str:
        value = match.group()[1:-1]
        if "\n" in value or not value:
            raise ValueError("multiline/empty escaped identifiers need a Lean declaration index")
        key = f"{prefix}{len(quoted)}"
        quoted[key] = value
        return key

    masked = re.sub(r"«[^»]*»", protect, masked)
    # In module mode declarations default to private. A public section changes
    # that default for its nested scopes; an explicit modifier still wins.
    module_mode = bool(re.match(rf"\s*module(?!{LEAN_ID_REST})", masked.lstrip("\ufeff")))
    if re.search(r"`\s*\(", masked) or re.search(
            r"^\s*(?:run_cmd|run_meta|run_elab|elab|macro|syntax)\b", masked, re.MULTILINE):
        raise ValueError("generated declarations/command quotations need a Lean declaration index")

    def parts(name: str) -> tuple[str, ...]:
        return tuple(quoted.get(part, part) for part in name_parts(name))

    namespace: tuple[str, ...] = ()
    scopes: list[tuple[str, tuple[str, ...], bool]] = []
    is_public = not module_mode
    declarations: dict[tuple[str, ...], str] = {}
    for match in COMMAND_RE.finditer(masked):
        scope = match.group("scope")
        modifiers = ATTRIBUTE_RE.sub("", match.group("mods")).split()
        if scope:
            body = match.group("body").strip()
            components = parts(body) if body else ()
            if scope == "namespace":
                if modifiers or not components:
                    raise ValueError("unsupported namespace command")
                for component in components:
                    scopes.append((component, namespace, is_public))
                    namespace += (component,)
            elif scope == "section":
                if any(mod not in {"public", "noncomputable", "meta"} for mod in modifiers):
                    raise ValueError("unsupported section modifiers")
                for component in components or ("",):
                    scopes.append((component, namespace, is_public))
                    is_public = is_public or "public" in modifiers
            else:
                ending = components or ("",)
                if modifiers or len(ending) > len(scopes) or tuple(
                        header for header, _, _ in scopes[-len(ending):]) != ending:
                    raise ValueError(f"end {body!r} does not match the active namespace/section")
                for _ in ending:
                    _, namespace, is_public = scopes.pop()
            continue
        if not include_private and ("private" in modifiers or (not is_public and "public" not in modifiers)):
            continue
        header = match.group("header")
        if match.group("kind") == "class" and re.match(r"inductive\s+", header):
            header = re.sub(r"^inductive\s+", "", header, count=1)
        if match.group("kind") == "instance" and header.startswith((":", "(", "{", "[")):
            continue
        ident = NAME_RE.match(header)
        tail = header[ident.end():] if ident else header
        if ident is None or (tail and not (tail[0].isspace() or tail[0] in ":({["
                                          or tail.startswith(".{"))):
            raise ValueError(f"unsupported declaration identifier in {header!r}")
        name = parts(ident.group())
        if name == ("_root_",):
            raise ValueError("_root_ is a namespace prefix, not a declaration name")
        declarations[name[1:] if name[0] == "_root_" else namespace + name] = match.group("kind")
    return declarations


def declaration_names(text: str) -> set[tuple[str, ...]]:
    return set(declaration_kinds(text))
