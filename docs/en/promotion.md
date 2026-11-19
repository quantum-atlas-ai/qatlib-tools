# How Shared Work Enters a Public Library

## Division of Responsibilities with Per-Repository Promotion Guides

This guide describes the general technical workflow by which shared work moves from an incubation repository into a staging validation repository, and subsequently enters a public library as part of an official release. Mechanisms shared across projects—whole-file dependency collection, consumer probes written as ordinary Lean modules, incremental bibliography merging, and cross-library conflict checks—are documented centrally in this handbook. Concrete directory layouts, metadata requirements, review roles, and admission criteria are maintained by each repository's own `promote` skill, which links back to this handbook.

## Phase 1: Migrating Whole Files and Validating in the Staging Repository

Migration starts from the modules slated for publication and recursively collects in-project dependencies along the `import` chain, distinguishing which modules are already provided by the receiving repository or its external dependencies. Source code should be migrated as complete `.lean` files, preserving all instances, documentation comments, and private helper lemmas within each file. If a dependency is missing, or if a different implementation already exists at the target path in the receiving repository, resolve the interface conflict before continuing the migration; for dependencies and unmigrated modules left behind in the source repository, record the reasons so the receiving side can review them.

When a project adapter is available, you can call the Python interface `qatlib_search.promotion_closure.candidate_closure` to help assemble the migration closure: the caller first verifies the dependency lock, then passes the candidate modules, source library root, and applicable import rules. This interface parses source code strictly in read-only mode. Under normal conditions, it returns a dependency-first list of whole files along with each file's import relationships; if any error is detected, it returns only the diagnostics and the import relationships read so far, leaving the migration list empty.

When synchronizing bibliographic references, first run `qatlib-env promotion merge-bibliography --increment /path/to/increment/references.bib` from the root of the staging repository as a merge precheck. When `--write` is passed, the tool preserves the existing bytes of the target `docs/references.bib` verbatim and appends only the raw text of entries not yet included; if it encounters a conflicting entry where the same citation key maps to different content, it blocks the write. Meanwhile, the incremental repository continues to validate references jointly against the locked public bibliography and its own incremental file; see [Incremental Bibliography](lean-docs.md#bibliography) for the detailed mechanism.

Beyond running the project's static checks and build, staging validation must verify the actually exposed interfaces from the perspective of an external ordinary `module`. For example, you can import and exercise the candidate module in `verification/Consumer.lean`:

```lean
module
import MyLib.Facts

#check MyLib.Facts.some_fact
```

```bash
qatlib-env promotion smoke --probe verification/Consumer.lean
```

A consumer probe should cover realistic call patterns with minimal imports: restate key theorem signatures with `example`, invoke functions promised to be computable from an ordinary `def`, and verify definitions promised to unfold using `rfl` or restricted `simp`. Using plain `import` ensures that the probe strictly respects the visibility boundaries downstream consumers actually face (see [Module Visibility](build.md#modules)). At runtime, `smoke` checks the target Lean file against existing dependency artifacts, records the current `HEAD`, toolchain, and diagnostics, and classifies the result as `PASS`, `REGRESSION`, or `NOT-RUN`. When intentional interface renames are involved, pass a migration map via `--migration-map migration.toml`, recording `old`, `new`, and `reason` in `[[rename]]` table entries.

<a id="check-export"></a>

### Checking Development-Only Legacy Interfaces

When refactoring and renaming, the source repository may—per project conventions—retain legacy aliases or test-forwarding files for one release cycle to leave migration headroom for work in progress. Such transitional items must be explicitly marked with a standalone line comment `-- promotion: dev-only`, and their old names, new names, and paths must be recorded in the current batch's migration list. When exporting to the receiving repository, strip all marked temporary aliases and forwarding files and update consumers to use the new interfaces; `@[deprecated]` declarations maintained by the public library under its own compatibility policy may remain as usual.

```bash
qatlib-env promotion check-export MyLib/Facts.lean verification/Consumer.lean
```

When running this check, explicitly pass every target Lean file exported in the current batch to the command, including new files not yet tracked by Git. The tool scans genuine Lean line comments strictly in read-only mode, automatically ignoring example text inside block comments and string literals; as soon as a residual marker is detected, it prints the corresponding file, line number, and error message and exits with a nonzero code. After the scan passes, cross-check the old names and paths against the migration list to confirm that no transitional items were left unmarked, and finally run the ordinary-module consumer probe.

## Rehearsing the Downstream Switch Ahead of Release

Once the changes in the upstream receiving package land on a staging branch, downstream repositories can open a Draft PR ahead of time that references the candidate dependency on a draft branch—rehearsing import paths, qualified names, consumer probes, and duplicate checks early while documenting the candidate commit and staging validation results according to project rules. This surfaces and addresses consumer compatibility issues before the official public version is released. Downstream interface switches and source removals may be merged only after both the official public release and its corresponding precompiled artifacts are fully available; until then, the downstream main branch retains its existing implementations and dependency lock.

## Phase 2: Updating Dependency Locks to the Official Release and Retiring Old Copies

Before making the formal switch, confirm that the official upstream release, its full commit SHA, and the corresponding precompiled artifacts can all be retrieved normally. Then switch the downstream draft to the official dependency and review the targeted version update and lockfile diff:

```bash
qatlib-env promotion bump-lock --package MyLib
qatlib-env promotion verify-lock --package MyLib
qatlib-env prepare
```

Here, `bump-lock` updates an existing direct Git dependency and verifies the updated local checkout, while `prepare` prepares dependency sources and caches according to the lockfile. Because `verify-lock` reads the locked local dependency sources, in a clean environment where dependencies have not yet been checked out, run `prepare` first and then run `verify-lock` to verify the lock state. The entire workflow must ensure that the locked commit SHA, cache preparation, and build results strictly correspond to the same version.

Within the same downstream PR, switch `import` and `open` statements, fully qualified names, tests, and project metadata over to the upstream public interfaces, while retiring local legacy sources, temporary aliases, and empty shell modules. After confirming that all local bibliography keys slated for deletion are already included in full upstream, check reverse references in unmodified files. Finally, run the following checks against the updated dependency lock:

```bash
qatlib-env promotion check-retired --package MyLib
qatlib-env conflicts --package MyLib
```

| Check | What It Verifies |
| --- | --- |
| `verify-lock` | The full Git SHA locked in the manifest matches the actual checkout of that dependency |
| `prepare` | Prepares dependency sources and configured caches according to the lockfile |
| `check-retired` | Matches by library root to find residual local modules at identical paths and duplicate explicit public declarations |
| `conflicts` | Scans for conflicts across cross-library modules, explicit public declarations, and global syntax; legitimate overloads require separate review |
| Project static checks, build, and consumer probes | Verifies directory layout, references, metadata, and interface usability per each repository's standards; can be integrated into project workflows via `qatlib-env ci lint` and `qatlib-env ci build` |

Only by switching the dependency lock, interface call sites, retired local copies, and bibliography changes together in a single validated PR can the downstream main branch continuously provide complete, usable interfaces. All commands above are independent preparation and verification steps; fix any failures before merging.
