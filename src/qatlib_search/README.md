# qatlib_search

`qatlib_search` provides Lean source masking, declaration and docstring parsing, import-closure analysis, promotion checks, proof indexes, retrieval and goal probes. It is the search library behind `qatlib-search`.

The parser handles nested comments, strings, tactic macros, quoted identifiers and guillemet imports. Source roots, material roots, metadata directories and index locations are explicit configuration values; the package does not assume a consumer repository layout.

Commands:

```text
qatlib-search build <root> -o <index.json>
qatlib-search query <index.json> <term> [--kind theorem] [--limit N]
qatlib-search retrieve <configured retrieval arguments>
python -m qatlib_search --help
```

The wheel includes `ExportSearchIndex.lean` and `retrieval-report.schema.json` as package data.
