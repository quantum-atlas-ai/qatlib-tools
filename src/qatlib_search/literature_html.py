"""Render one offline page with links between literature records."""
from __future__ import annotations

from collections import defaultdict
from html import escape
from urllib.parse import quote

from .literature import Snapshot, paper_identifiers, source_link


def _link(kind: str, key: str, label: str | None = None) -> str:
    return f'<a href="#{kind}-{quote(key, safe="")}">{escape(key if label is None else label)}</a>'


def _list(items) -> str:
    items = list(items)
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>" if items else ""


def _version_link(paper: dict, version: str) -> str:
    return _link("version", paper["arxiv_id"] + version, version)


def _external_reference(url: str) -> str:
    return f"<code>{escape(url)}</code>"


def _paper(paper: dict) -> list[str]:
    ident = paper["paper_id"]
    lines = [f'<article><h3 id="paper-{escape(ident)}">{escape(paper["title"])}</h3>',
             _list(f'<span id="identifier-{escape(key)}"><code>{escape(key)}</code></span>'
                   for key in dict.fromkeys(paper_identifiers(paper)))]
    for source in paper["sources"]:
        revision = source["version"]
        arxiv = paper["arxiv_id"] + revision
        lines.extend((
            f'<h4 id="version-{escape(arxiv)}">arXiv:{escape(arxiv)}</h4>',
            f'<p>{_external_reference(source["url"])} · SHA-256 <code>{escape(source["sha256"])}</code></p>',
        ))
        for claim in paper["claims"]:
            if claim["version"] == revision:
                locators = ", ".join(map(escape, claim["locators"]))
                lines.extend((
                    f'<h5 id="claim-{escape(claim["id"])}">{locators}</h5>',
                    f'<p>{escape(claim["summary"])} · {escape(claim["coverage"])}</p>',
                    _list(_link("interface", fqn) for fqn in claim["interfaces"]),
                ))
        linked = [finding for finding in paper["errata"]
                  if any(item["version"] == revision for item in [*finding["affected"], *finding["fixed_in"]])]
        lines.append(_list(_link("erratum", item["id"], item["summary"]) for item in linked))
    return [*lines, "</article>"]


def render_html(snapshot: Snapshot) -> bytes:
    manifest = snapshot.manifest
    lines = [
        "<!doctype html>", '<html lang="en"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
        "<title>Literature reverse index</title>",
        "<style>body{max-width:72rem;margin:auto;padding:1.5rem;font:1rem/1.55 system-ui;overflow-wrap:anywhere}"
        "pre{white-space:pre-wrap}a{color:#164c84}a:focus-visible{outline:2px solid}"
        ":target{background:#fff2bd}article{border-bottom:1px solid #888;padding-bottom:1rem}</style>",
        '<body><a href="#main">Skip to results</a><header><h1>Literature reverse index</h1>',
        f'<p>{escape(manifest["scope"])}</p>',
        f'<p>Snapshot <code>{manifest["snapshot_id"]}</code><br>Source commit <code>{manifest["source_commit"]}</code></p>',
        '<nav aria-label="Index sections"><a href="#papers">Papers</a> · '
        '<a href="#identifiers">Identifiers</a> · <a href="#interfaces">Interfaces</a> · '
        '<a href="#errata">Errata</a></nav></header><main id="main">',
        '<section aria-labelledby="papers"><h2 id="papers">Papers</h2>',
        _list(_link("paper", paper["paper_id"], paper["title"]) for paper in snapshot.papers),
    ]
    identifiers = {}
    by_fqn = defaultdict(list)
    for paper in snapshot.papers:
        lines.extend(_paper(paper))
        for ident in paper_identifiers(paper):
            identifiers[ident] = paper
        for item in paper["interfaces"]:
            by_fqn[item["fqn"]].append((paper, item))
    lines.extend(("</section>", '<section aria-labelledby="identifiers"><h2 id="identifiers">Identifiers</h2>',
                  _list(f'{_link("identifier", ident)} · {_link("paper", identifiers[ident]["paper_id"])}'
                        for ident in sorted(identifiers, key=str.casefold)),
                  "</section>", '<section aria-labelledby="interfaces"><h2 id="interfaces">Interfaces</h2>'))
    for fqn, entries in sorted(by_fqn.items()):
        lines.append(f'<h3 id="interface-{escape(fqn)}"><code>{escape(fqn)}</code></h3>')
        for paper, item in entries:
            signature = "\n".join([*item["variable_context"], item["source_signature"]])
            lines.extend((
                f'<p>{_link("paper", paper["paper_id"])} · {escape(item["coverage"])} · '
                f'{escape(item["machine_scope"])}</p>',
                f'<p>Proof audit: {escape(item["proof_audit"])} · Semantic review: {escape(item["semantic_review"])}</p>',
                f'<p><code>import {escape(item["module"])}</code></p>',
                f'<pre>{escape(signature)}</pre>',
                _list(map(escape, item["assumptions"])),
                f'<p>{_external_reference(source_link(manifest, item))}</p>',
                _list(_link("claim", claim["id"], ", ".join(claim["locators"]))
                      for claim in paper["claims"] if fqn in claim["interfaces"]),
                _list(_external_reference(url) for url in item["consumers"]),
            ))
    lines.extend(("</section>", '<section aria-labelledby="errata"><h2 id="errata">Errata</h2>'))
    for paper in snapshot.papers:
        for finding in paper["errata"]:
            lines.extend((
                f'<h3 id="erratum-{escape(finding["id"])}">{escape(finding["id"])}</h3>',
                f'<p>{_link("paper", paper["paper_id"])} · {escape(finding["summary"])}</p>',
                f'<p>Checked on <time>{escape(finding["checked_on"])}</time></p>',
            ))
            for key, label in (("affected", "Affected"), ("fixed_in", "Fixed in")):
                if finding[key]:
                    locations = ", ".join(
                        f'{_version_link(paper, location["version"])} ({", ".join(map(escape, location["locators"]))})'
                        for location in finding[key])
                    lines.append(f"<p>{label} {locations}</p>")
            if finding["correction_fqn"]:
                lines.append(f'<p>Correction {_link("interface", finding["correction_fqn"])}</p>')
            lines.append(_list(f'{escape(item["kind"])} · {escape(item["summary"])} · '
                               f'{_external_reference(item["url"])}' for item in finding["evidence"]))
    lines.extend(("</section></main></body></html>", ""))
    return "\n".join(lines).encode("utf-8")
