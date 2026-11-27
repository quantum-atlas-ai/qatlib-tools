import asyncio
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

import pytest

from qatlib_search.literature import INDEX_NAME, MANIFEST_NAME, LiteratureError, export_index, load_index
from qatlib_search.literature_cli import validate_findings
from qatlib_search.literature_html import render_html
from qatlib_search.literature_query import execute
from tests.test_public_hygiene import SAFETY_PATTERNS

FIXTURES = Path(__file__).parent / "fixtures" / "literature"
CURATION = FIXTURES / "public-curation.json"
CANDIDATE = FIXTURES / "contour-candidate.json"
CASES = [
    ("lookup-paper", {"identifier": "HTTPS://ARXIV.ORG/ABS/1806.01838v1"},
     ["HTTPS://ARXIV.ORG/ABS/1806.01838v1"], "MATCH"),
    ("lookup-paper", {"identifier": "GilyenSuLowWiebe2019", "version": "2"},
     ["GilyenSuLowWiebe2019", "--version", "2"], "SOURCE_VERSION_UNKNOWN"),
    ("lookup-paper", {"identifier": "arXiv:0000.00000"}, ["arXiv:0000.00000"], "NO_MATCH"),
    ("search-interface", {"oracle": "unitary and adjoint"},
     ["--oracle", "unitary and adjoint"], "RELATED_ONLY"),
    ("search-interface", {"resource_field": "adjointQueryCount", "limit": 1},
     ["--resource-field", "adjointQueryCount", "--limit", "1"], "RELATED_ONLY"),
    ("search-interface", {"text": "nonexistentinterface"}, ["nonexistentinterface"], "NO_MATCH"),
    ("check-errata", {"identifier": "CBMD", "version": "v4", "locator": "Equation(7)"},
     ["CBMD", "--version", "v4", "--locator", "Equation(7)"], "MATCH"),
    ("check-errata", {"identifier": "doi:10.1088/2058-9565/ae7b7e", "version": "v4"},
     ["doi:10.1088/2058-9565/ae7b7e", "--version", "v4"], "SOURCE_VERSION_UNKNOWN"),
]


@pytest.fixture
def snapshot_dir(tmp_path):
    directory = tmp_path / "snapshot"
    export_index(CURATION, directory)
    return directory


def cli(*args):
    return subprocess.run([sys.executable, "-m", "qatlib_search", *map(str, args)],
                          capture_output=True, text=True, timeout=20)


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_build_is_byte_stable_and_normalized(snapshot_dir, tmp_path):
    reordered = json.loads(CURATION.read_bytes())
    reordered["papers"].reverse()
    for paper in reordered["papers"]:
        paper["sources"].reverse()
    source = write_json(tmp_path / "reordered.json", reordered)
    other = tmp_path / "other"
    export_index(source, other)
    for name in (INDEX_NAME, MANIFEST_NAME):
        assert (snapshot_dir / name).read_bytes() == (other / name).read_bytes()
    snapshot = load_index(other)
    assert [paper["arxiv_id"] for paper in snapshot.papers] == ["1806.01838", "2511.10267"]
    assert snapshot.manifest["record_count"] == 2
    assert snapshot.manifest["source_commit"] == reordered["source_commit"]
    assert export_index(CURATION, other) == snapshot.manifest


@pytest.mark.parametrize("filename", [INDEX_NAME, MANIFEST_NAME])
def test_changed_snapshot_is_rejected_on_cli(snapshot_dir, filename):
    path = snapshot_dir / filename
    data = path.read_bytes()
    path.write_bytes(data.replace(b"2511", b"2512", 1) if filename == INDEX_NAME
                     else data.replace(b'"record_count":2', b'"record_count":3'))
    result = cli("lookup-paper", "QSVT", "--snapshot", snapshot_dir)
    assert result.returncode == 2 and result.stdout == ""
    assert "INDEX_STALE" in result.stderr


@pytest.mark.parametrize("mutation", ["alias", "version", "correction", "path", "type"])
def test_invalid_curated_records_are_rejected_before_writing(tmp_path, mutation):
    data = json.loads(CURATION.read_bytes())
    contour, qsvt = data["papers"]
    if mutation == "alias":
        contour["aliases"].append(qsvt["paper_id"].lower())
    elif mutation == "version":
        contour["sources"][0]["url"] = contour["sources"][1]["url"]
    elif mutation == "correction":
        contour["errata"][0]["correction_fqn"] = "Unmapped.correction"
    elif mutation == "path":
        contour["interfaces"][0]["path"] = "../Outside.lean"
    else:
        contour["interfaces"][0]["type"] = "Unchecked → Unchecked"
    source = write_json(tmp_path / "input.json", data)
    output = tmp_path / "output"
    with pytest.raises(LiteratureError) as error:
        export_index(source, output)
    assert error.value.code == ("AMBIGUOUS_ID" if mutation == "alias" else "INVALID_ARGUMENT")
    assert not output.exists()


def test_aliases_separate_publisher_identity_and_version_scope(snapshot_dir):
    snapshot = load_index(snapshot_dir)
    by_arxiv = execute(snapshot, "lookup-paper", {"identifier": "1806.01838v1"})
    by_alias = execute(snapshot, "lookup-paper", {"identifier": "gilyensulowwiebe2019", "version": "v1"})
    assert by_arxiv["results"] == by_alias["results"]
    doi = execute(snapshot, "lookup-paper", {"identifier": "https://doi.org/10.1145/3313276.3316366"})
    assert doi["status"] == "MATCH" and doi["results"][0]["selected_source"] is None
    assert doi["results"][0]["errata"] == []
    exact = execute(snapshot, "check-errata", {"identifier": "2511.10267v3", "locator": "Eq. 7"})
    assert exact["results"][0]["disposition"] == "AFFECTED"
    fixed = execute(snapshot, "check-errata", {"identifier": "2511.10267v4", "locator": "Eq. 7"})
    assert fixed["results"][0]["disposition"] == "FIXED_IN"
    assert execute(snapshot, "check-errata", {"identifier": "2511.10267v3", "locator": "Eq. 70"})["status"] == "NO_MATCH"
    unknown = execute(snapshot, "check-errata", {"identifier": "2511.10267"})
    assert unknown["status"] == "SOURCE_VERSION_UNKNOWN" and unknown["known_versions"] == ["v3", "v4"]
    with pytest.raises(LiteratureError, match="different revisions"):
        execute(snapshot, "lookup-paper", {"identifier": "2511.10267v3", "version": "v4"})
    fqn = by_arxiv["results"][0]["interfaces"][0]["fqn"]
    hit = execute(snapshot, "search-interface", {"text": fqn})
    assert hit["status"] == "RELATED_ONLY" and hit["results"][0]["coverage"] == "restricted"


def test_mcp_client_and_cli_return_identical_json(snapshot_dir, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check(stderr):
        server = StdioServerParameters(command=sys.executable, args=[
            "-m", "qatlib_search", "literature-mcp", "--snapshot", str(snapshot_dir)
        ])
        async with stdio_client(server, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert set(tools) == {"qatlib_lookup_paper", "qatlib_search_interface", "qatlib_check_errata"}
                for command, arguments, flags, status in CASES:
                    completed = cli(command, "--snapshot", snapshot_dir, *flags)
                    assert completed.returncode == 0 and completed.stderr == ""
                    name = "qatlib_" + command.replace("-", "_")
                    tool = tools[name]
                    assert tool.annotations.readOnlyHint and tool.inputSchema and tool.outputSchema
                    result = await client.call_tool(name, arguments)
                    assert not result.isError
                    assert result.structuredContent["status"] == status
                    assert result.structuredContent == json.loads(completed.stdout)
                    assert json.loads(result.content[0].text) == result.structuredContent
                invalid = await client.call_tool("qatlib_search_interface", {"limit": 0})
                assert invalid.isError

    log = tmp_path / "mcp.stderr"
    with log.open("w", encoding="utf-8") as stderr:
        asyncio.run(asyncio.wait_for(check(stderr), 45))
    assert not log.read_bytes()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.references = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                self.ids.append(value)
            if key in {"href", "src", "action", "srcset"}:
                self.references.append(value)


def test_reverse_index_is_offline_deterministic_and_linked(snapshot_dir, tmp_path):
    output = tmp_path / "index.html"
    result = cli("export-literature-html", "--snapshot", snapshot_dir, "--output", output)
    assert result.returncode == 0 and not result.stderr
    html = output.read_bytes()
    assert html == render_html(load_index(snapshot_dir))
    links = Links()
    links.feed(html.decode("utf-8"))
    assert len(links.ids) == len(set(links.ids))
    assert all(href.startswith("#") and unquote(href[1:]) in links.ids for href in links.references)
    assert {"paper-QSVT", "identifier-10.1145/3313276.3316366", "version-2511.10267v4",
            "claim-qsvt-theorem-73", "erratum-contour-inner-lcu-phase"} <= set(links.ids)
    assert any(href.startswith("#interface-") for href in links.references)


def test_candidate_files_append_independently(tmp_path):
    another = json.loads(CANDIDATE.read_bytes())
    another["id"] = "next-finding"
    path = write_json(tmp_path / "next.json", another)
    result = cli("validate-literature-finding", CANDIDATE, path)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout)["ids"] == ["contour-inner-phase-candidate", "next-finding"]
    with pytest.raises(LiteratureError, match="duplicate"):
        validate_findings([CANDIDATE, CANDIDATE])


@pytest.mark.parametrize("missing", ["version", "locator", "evidence", "logic_kernel",
                                     "source_alignment", "scientific_semantics", "pinned_url"])
def test_candidate_requires_version_locator_evidence_and_gates(tmp_path, missing):
    candidate = json.loads(CANDIDATE.read_bytes())
    if missing == "pinned_url":
        candidate["source"]["url"] = "https://arxiv.org/pdf/2511.10267"
    elif missing in candidate["source"]:
        candidate["source"].pop(missing)
    elif missing in candidate["gates"]:
        candidate["gates"].pop(missing)
    else:
        candidate.pop(missing)
    with pytest.raises(LiteratureError):
        validate_findings([write_json(tmp_path / "candidate.json", candidate)])


def test_fixture_and_generated_documents_have_only_approved_public_sources(snapshot_dir):
    snapshot = load_index(snapshot_dir)
    assert [(paper["arxiv_id"], [row["version"] for row in paper["sources"]])
            for paper in snapshot.papers] == [("1806.01838", ["v1"]), ("2511.10267", ["v3", "v4"])]
    patterns = [*SAFETY_PATTERNS.values(),
                re.compile("|".join(("qatlib" + "-dev", r"\.agent" + "-tasks")), re.I)]
    texts = [path.read_text(encoding="utf-8") for path in
             (CURATION, CANDIDATE, snapshot_dir / INDEX_NAME, snapshot_dir / MANIFEST_NAME)]
    texts.append(render_html(snapshot).decode("utf-8"))
    urls = set(re.findall(r'https://[^"\s<>]+', "\n".join(texts)))
    public_tree = snapshot.manifest["source_repository"] + "/blob/" + snapshot.manifest["source_commit"] + "/"
    assert all(url.startswith(("https://arxiv.org/pdf/", public_tree)) or
               url == snapshot.manifest["source_repository"] for url in urls)
    assert all(not pattern.search(text) for pattern in patterns for text in texts)
