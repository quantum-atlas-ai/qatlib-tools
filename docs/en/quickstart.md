# Five-minute check

This page has one goal: in a **checkout of this tools repository**, verify that `qatlib-env` is installed and can read the handbook and the local configuration. It does not download a Lean toolchain or compile Lean.

Start in the checkout root of `qatlib-tools`, where `pyproject.toml` and `docs/` are visible. Make sure `uv` and Python 3.11 or newer are already installed (GitHub CI for this repository uses Python 3.12).

## Install this checkout

From the checkout root, run:

```bash
uv tool install .
```

`uv` sets up a separate tool environment for `qatlib-env`, `qatlib-retrieve`, `qatlib-forge`, and `qatlib-lean-tools`. This writes to the `uv` tool environment and leaves the source files in the checkout intact.

From a Lake TOML project, use `qatlib-lean-tools retrieve --prefix MyLib.` for qualified names, `qatlib-lean-tools retrieve --substring identity` for a literal name fragment, or `qatlib-lean-tools retrieve --module MyLib.Basic --kind theorem --json` for theorems defined in one module. Use `--project PATH` to select a checkout. The command reads project libraries and available locked TOML dependencies directly and reports lexical coverage gaps as warnings. Signatures are the written source headers, with enclosing variables in `variable_context`.

Use `qatlib-lean-tools env --project PATH --json` to inspect the initial toolchain, linked worktree, and artifact layout without preparing the checkout. Cache layout indicates presence. Lake validates the requested artifacts during `qatlib-lean-tools env run --project PATH --json -- lake --no-build lean File.lean`. A cache miss fails the native check. Ordinary builds still use the cold-module budget. `env prepare --project PATH --json` records dependency preparation and cache recovery. Each report contains `phase_timings` in milliseconds, with `null` for phases that did not run. Child output streams to stderr in JSON mode.

Once installation succeeds, open a new terminal and run `qatlib-env --help`; the help text should include `docs`. If your shell cannot find the command, follow the `PATH` note in [Install](install.md) to add the directory printed by `uv tool dir --bin`, then return here.

For other installation options—pinning a specific commit, installing a wheel, adjusting Windows paths, or installing the version locked by a downstream Lean repository—see [Install](install.md). For this check, the command above is all you need.

## Read the topic list

```bash
qatlib-env docs --list
```

On success, standard output prints one topic name per line without a `.md` suffix, including `quickstart`, `worktrees`, `pull-requests`, and `github-ci`. Those names correspond to the Chinese topics under `docs/`. To read a topic, pass its name (for example, `qatlib-env docs worktrees`).

Running `qatlib-env docs` with no topic prints `README.md` from the repository root. Note that this offline command prints the Chinese text; the English pages live on the site under `/en/`.

## See where the configuration comes from

```bash
qatlib-env config explain --json
```

On success, the command exits with status 0 and prints a single JSON object to standard output. Review these fields:

| Field | What it tells you |
| --- | --- |
| `resources` | Detected memory, CPUs, and visible limits |
| `automatic` | Values derived before extra overrides |
| `values` | Parameters in effect for this invocation |
| `configuration.routing` | Routing and repository configuration in effect |
| `sources` and `configuration.sources` | Which layer supplied each value |
| `warnings` | Messages to review; an empty array means no warnings |
| `paths` | Locations under `QATLIB_HOME` (default `~/.qatlib`) |

This command does not create those directories, fetch dependencies, or acquire a build-pool lease. Once the JSON appears on screen, you can stop. There is nothing to edit on this page.

## How long the first Lean build takes

With the three steps above complete, the tools themselves are verified. In a Lean repository, the first `qatlib-env prepare` or `lake build` is the step that may download a Lean toolchain, fetch locked dependency sources, and unpack or compile artifacts. That run often takes much longer than these five minutes, though it is shorter when the shared cache already holds the dependencies. Continue with [A second worktree](worktrees.md).

## Where to go next

- To edit a Lean repository, open [A second worktree](worktrees.md).
- To make changes to `qatlib-tools` itself, open [Pull requests](pull-requests.md). You can find the local self-test commands in the [contributing guide](../../CONTRIBUTING.en.md#self-test).
