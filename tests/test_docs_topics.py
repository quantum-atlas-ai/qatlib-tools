"""Offline topic list stays aligned with the site map and local links."""
from __future__ import annotations

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qatlib_env.workspace import main


ROOT = Path(__file__).resolve().parents[1]
TOPICS_RE = re.compile(r"const TOPICS = \[(.*?)\];", re.S)
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
ID_RE = re.compile(r"<a\s+id=\"([A-Za-z0-9_-]+)\"")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


def published_topics():
    """Match qatlib-env docs --list: filename order, not stem order.

    ``runner-troubleshooting.md`` sorts before ``runner.md`` because ``-``
    precedes ``.``.
    """
    return [path.stem for path in sorted((ROOT / "docs").glob("*.md"))]


def declared_topics():
    match = TOPICS_RE.search((ROOT / "site/scripts/sync-docs.mjs").read_text(encoding="utf-8"))
    assert match, "sync-docs.mjs must declare const TOPICS"
    return re.findall(r"'([a-z0-9-]+)'", match.group(1))


def heading_ids(text: str) -> set[str]:
    found = set(ID_RE.findall(text))
    for heading in HEADING_RE.findall(text):
        title = re.sub(r"<[^>]+>", "", heading[1])
        title = re.sub(r"`+", "", title).strip().lower()
        slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug).strip("-")
        if slug:
            found.add(slug)
    return found


def test_docs_list_matches_chinese_topics_and_the_site_map(capsys):
    topics = published_topics()
    english = [path.stem for path in sorted((ROOT / "docs" / "en").glob("*.md"))]
    assert english == topics
    assert main(["docs", "--list"]) == 0
    assert capsys.readouterr().out.splitlines() == topics
    declared = declared_topics()
    assert sorted(declared) == sorted(topics)
    assert len(declared) == len(set(declared))
    config = (ROOT / "site/.vitepress/config.mts").read_text(encoding="utf-8")
    assert sorted(set(re.findall(r"'/docs/([a-z0-9-]+)'", config))) == sorted(topics)
    assert sorted(set(re.findall(r"'/en/docs/([a-z0-9-]+)'", config))) == sorted(topics)
    for name in ("quickstart", "worktrees", "pull-requests", "github-ci", "install", "runner-troubleshooting"):
        assert name in topics


def test_handbook_relative_links_resolve_to_files_and_anchors():
    roots = [
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.en.md",
        ROOT / "DESIGN.md",
        ROOT / "DESIGN.en.md",
        ROOT / "SECURITY.md",
        * (ROOT / "docs").rglob("*.md"),
        * (ROOT / ".github").rglob("*.md"),
    ]
    missing = []
    for path in roots:
        text = path.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            raw, _, fragment = target.partition("#")
            if not raw:
                if fragment and fragment not in heading_ids(text):
                    missing.append(f"{path.relative_to(ROOT)}: #{fragment}")
                continue
            destination = (path.parent / raw).resolve()
            try:
                destination.relative_to(ROOT)
            except ValueError:
                missing.append(f"{path.relative_to(ROOT)}: escapes repo {target}")
                continue
            if not destination.is_file():
                missing.append(f"{path.relative_to(ROOT)}: missing {target}")
                continue
            if fragment and destination.suffix == ".md":
                if fragment not in heading_ids(destination.read_text(encoding="utf-8")):
                    missing.append(f"{path.relative_to(ROOT)}: missing anchor {target}")
    assert missing == []
