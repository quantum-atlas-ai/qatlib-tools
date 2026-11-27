# Lean Documentation, Bibliography, and Source Retrieval

The working directory is the root of a Lean project whose locked dependencies are already checked out. Build the index, run one query, and open the source the hit names. Website rendering, bibliography merges, and the legacy index path come after the query.

<a id="lexical-index"></a>

## Index, then query, then the source

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

`index` succeeds when the process exits 0 without launching the Lean compiler or Lake, and without fetching missing dependencies or requesting build quota. `query` succeeds when it prints the location of a matching declaration. Open that file and compare the source signature in the hit, and the `variable` declarations in the enclosing `section`, with the implicit assumptions you expect. The query strings above are examples. Replace them with the words or citation key you are looking for.

`qatlib-retrieve index` statically scans the local canonical libraries defined in Lake TOML, supported Git dependencies already checked out from the lock, and `docs/references.bib` in each of those repositories. Dependencies that are not checked out yet are skipped with a notice. Scanning starts at each source repository's canonical library roots and excludes `.git`, `.lake`, `.venv`, and `.agent-tasks`. After the first index, use `qatlib-retrieve index --changed` for an incremental update.

The default index path is `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`, where `<corpus-key>` is the first 24 hexadecimal characters of the SHA-256 of the resolved absolute project root. Select a project with `--project`, or another index file with `--db`. The chosen path has to lie outside every indexed source repository. Before the new index exists, a query can open the matching file in the legacy directory `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/` read-only. Running `index` rebuilds into the new directory. If only a legacy index is present, the first `index --changed` also rebuilds fully in the new directory and keeps the old file.

`query` and `find` open an existing index read-only. Rebuild when the format is old. Macro expansions, generated structure fields, and compiler-derived declarations may be absent. Lean compilation and a consumer check remain the authority.

## Versioned literature snapshots

From a Tools checkout, build a public example containing `index.jsonl`, `manifest.json` and an offline reverse index. Literature queries need only the first two files.

```bash
uv sync --locked --extra literature
uv run qatlib-retrieve export-literature-index tests/fixtures/literature/public-curation.json --output dist/literature
uv run qatlib-retrieve lookup-paper --snapshot dist/literature arXiv:1806.01838v1
uv run qatlib-retrieve search-interface --snapshot dist/literature --resource-field adjointQueryCount
uv run qatlib-retrieve check-errata --snapshot dist/literature 2511.10267v4 --locator 'Eq. (7)'
uv run qatlib-retrieve export-literature-html --snapshot dist/literature --output dist/literature/index.html
```

Select a curation file approved for public distribution. The manifest pins the public source commit, Lean toolchain, dependency-lock hash, data schema and index SHA-256. Its source date is the commit's UTC date. Rebuilding uses no wall clock. To verify Lean declarations, build the cache with `qatlib-retrieve index --project CHECKOUT --db CACHE` at the pinned commit and add `--project CHECKOUT --db CACHE` to the literature export command. Run a full `index` if an existing cache reports a scanner change.

| Status | Meaning |
| --- | --- |
| `MATCH` | The bibliographic identity, selected source version or version-specific finding is indexed. Findings distinguish `AFFECTED` versions from `FIXED_IN` revisions. |
| `NO_MATCH` | The current snapshot has no matching record. |
| `RELATED_ONLY` | Lexical Lean candidates. Check their types, premises and consumers before use. |
| `SOURCE_VERSION_UNKNOWN` | The paper is indexed and the queried source version is unresolved. A publisher DOI identifies the work. Each source text requires separate verification. |

Query JSON goes to stdout and errors go to stderr. `source_signature` and `variable_context` capture the written Lean header and enclosing variables. Elaborated types and audit or review attestations may be absent, as shown by `null` or `not_attested`.

Configure a local MCP client to run `qatlib-retrieve literature-mcp --snapshot SNAPSHOT` in an environment with `qatlib-tools[literature]` installed. The stdio server exposes `qatlib_lookup_paper`, `qatlib_search_interface` and `qatlib_check_errata`. Each tool advertises its input schema and returns the same structured response as the corresponding CLI command.

Use `.github/PULL_REQUEST_TEMPLATE/literature-finding.md` for external contributions, with one JSON file per finding. Run `qatlib-retrieve validate-literature-finding RECORD.json` and supply multiple files to check IDs for uniqueness. Admission to the public snapshot requires a Lean build and transitive axiom audit, review of the source alignment, and review of the scientific semantics.

<a id="doc-gen"></a>

## Documentation comments and website links

Lean source uses two documentation comments. `/-! ... -/` is the module comment, for the file's job, its main definitions, and its dependencies. `/-- ... -/` sits on one declaration and records its meaning, assumptions, and use. In a traditional Markdown comment, wrap a fully qualified name in backticks, for example `` `MyLib.Facts.some_fact` ``. When the website is generated, doc-gen4 resolves the link from the analyzed environment and symbol table (see the [doc-gen4 analysis implementation](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Process/Analyze.lean#L164-L171) and [docstring link rendering](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/DocString.lean#L167-L228)). Lean LSP hover in an editor renders the local comment only. It has no site router and no bibliography index. To open the paper from hover as well, add an ordinary Markdown link to the DOI or arXiv URL.

A file with Verso enabled can use the `{name}` role and let the compiler resolve the name. Traditional comments can sit beside a Verso pilot. Before widening that use, check separately that a misspelled name is caught by the compiler, that hover still renders, that the website link lands on the right page, and that a template summary does not show the raw markup.

When building the project documentation site, reuse an HTML/JSON snapshot that matches the current source and tool versions. If restoring the snapshot fails, regenerate it and check the output. A change that only edits portal copy can be checked on the page and its links.

<a id="bibliography"></a>

## Citations and incremental merges

Cite `docs/references.bib` with `[CiteKey]`. To point at a place in the work, add a theorem number, page, or section after the key, for example `[CiteKey], Theorem X`. Generated bibliography links look like `references.html#ref_<CiteKey>` (see the [doc-gen4 bibliography page](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/References.lean#L75-L84)). A handwritten link should be compared with the `href` and `id` in the final HTML. Whether a formalized statement matches the paper is settled by reading the paper.

A development repository can keep only an incremental bibliography day to day. Validation overlays the locked public baseline and checks missing keys, duplicate keys in one file, and the same key with different text. When merging into the receiving repository, precheck there with `qatlib-env promotion merge-bibliography --increment <file>`, then add `--write` after it passes. The command keeps the existing bytes of `docs/references.bib` and appends only the raw text of new entries. After the downstream lock moves forward, remove incremental entries that are already present with the same text, then search for the citations again.
