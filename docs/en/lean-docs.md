# Lean Documentation, Bibliography, and Source Retrieval

<a id="doc-gen"></a>

## Documentation Comments and Website Links

Lean source files organize documentation using two kinds of documentation comments: `/-! ... -/` provides module-level documentation suited for outlining a file's responsibilities, core definitions, and dependencies; `/-- ... -/` attaches directly to an individual declaration to document its semantics, preconditions, and usage. In traditional Markdown comments, you can wrap a declaration's fully qualified name in backticks (for example, `MyLib.Facts.some_fact`); when generating the website, doc-gen4 resolves link targets against the analyzed environment and symbol table (see the [doc-gen4 analysis implementation](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Process/Analyze.lean#L164-L171) and [docstring link rendering](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/DocString.lean#L167-L228)). By contrast, Lean LSP hover in the editor renders only local documentation without the website's routing mechanism or bibliography index; if you want the corresponding paper to open directly from hover as well, use a standard Markdown link pointing to its DOI or arXiv URL.

In files with Verso enabled, you can use the `{name}` role so that the compiler resolves declaration names. Traditional comments can coexist with Verso pilots; before expanding adoption, verify separately that misspelled names are caught by the compiler, editor hover renders properly, website links navigate to the intended targets, and template summaries do not display raw markup directly.

When building the project documentation site, you can reuse HTML/JSON snapshots that match the current source tree and tool versions; if snapshot restoration fails, regenerate them and verify the output. If a change affects only portal copy, inspecting the corresponding pages and links directly is sufficient.

<a id="bibliography"></a>

## Bibliography Citations and Incremental Merging

Documentation can cite entries in `docs/references.bib` via `[CiteKey]`; to pinpoint a specific location, append the theorem number, page number, or section after the citation key, such as `[CiteKey], Theorem X`. Site-generated bibliography links take the form `references.html#ref_<CiteKey>` (see the [doc-gen4 bibliography page implementation](https://github.com/leanprover/doc-gen4/blob/0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae/DocGen4/Output/References.lean#L75-L84)), so any handwritten links should be checked against the `href` and `id` attributes in the final HTML. Verifying that a formalized theorem matches the statement in the paper still requires reading the original source text.

In day-to-day development, a development repository can maintain only an incremental bibliography; validation overlays it onto the locked public baseline to check for missing keys, duplicate keys within the same file, and conflicting entries under the same key. When merging into a receiving repository, first run a precheck in the receiving repository with `qatlib-env promotion merge-bibliography --increment <file>`, then add `--write` to apply the changes once validation passes; this command preserves the existing bytes of `docs/references.bib` verbatim and appends only the raw text of new entries. Once downstream dependency locks are updated, incremental entries that have already been incorporated with identical content can be pruned, followed by cross-checking citations.

<a id="lexical-index"></a>

## Local Indexing and Retrieval

`qatlib-retrieve index` statically scans the local canonical libraries defined in Lake TOML, supported Git dependencies already checked out according to the lockfile, and `docs/references.bib` in each of these repositories; dependencies not yet checked out are skipped with a notice. The entire indexing process runs without launching the Lean compiler or Lake, and neither accesses the network to fetch missing dependencies nor requests build quota. Scanning is rooted at the canonical library roots of each source repository and automatically excludes `.git`, `.lake`, `.venv`, and `.agent-tasks`. After building the index for the first time, use `qatlib-retrieve index --changed` for subsequent incremental updates.

The default index path is `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`, where `<corpus-key>` is the first 24 hexadecimal characters of the SHA-256 hash of the resolved absolute project root path. You can select a project with `--project` or specify an alternate index file with `--db`, provided the chosen path lies outside every indexed source repository. When the new index has not yet been created, query commands can open the corresponding file in the legacy directory `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/` in read-only mode; running `index` directly rebuilds the index in the new directory, and if only a legacy index exists locally, running `index --changed` for the first time likewise performs a full rebuild in the new directory while preserving the legacy file.

`query` and `find` access existing indexes strictly in read-only mode; if an older index format is encountered, simply rebuild the index. During retrieval, inspect the source signatures and enclosing `section` `variable` declarations included in search hits to check for duplicates and verify implicit assumptions before opening the source files to confirm. Because indexing relies on static scanning, macro expansions, auto-generated structure declarations, and compiler-derived declarations may be omitted; Lean compilation and consumer verification remain the final authority.
