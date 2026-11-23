# Contributing

Before changing a shared tool, check which version the projects that call it have locked, and how they call it. Each command does one job so it can be combined with the others. Rules and acceptance criteria for a project stay in that project.

The design principles and the human-review agreement are in [DESIGN.en.md](DESIGN.en.md). A change to a protected block, its markers, or `docs/design/human-contracts.sha256` starts as a draft and follows the process in that file.

<a id="self-test"></a>

## Local self-test matching CI

The working directory is the root of a checkout of this tools repository. Use Python 3.12. The order matches the `test` job in `.github/workflows/ci.yml`. CI appends `--draft` to the contract script for a draft pull request. When you are about to mark the pull request ready, run the check locally without `--draft`.

```bash
uv run --no-project python scripts/check_human_contracts.py
uv lock --check
uv build
uv run --with pytest --with hatchling --with jsonschema pytest -q -rs
uv venv --python 3.12 .wheel-venv-ci
uv pip install --python .wheel-venv-ci/bin/python dist/*.whl
.wheel-venv-ci/bin/qatlib-env --help
.wheel-venv-ci/bin/qatlib-env exec --help
.wheel-venv-ci/bin/qatlib-env promotion merge-bibliography --help
.wheel-venv-ci/bin/qatlib-env docs > .wheel-venv-ci/manual.md
cmp README.md .wheel-venv-ci/manual.md
.wheel-venv-ci/bin/qatlib-forge --help
.wheel-venv-ci/bin/qatlib-retrieve --help
.wheel-venv-ci/bin/python -m qatlib_search --help
.wheel-venv-ci/bin/python -m qatlib_env.ci --help
```

On success, every command exits 0. `cmp` prints nothing, so the manual installed in the clean environment has the same bytes as the current `README.md`. Each line of `qatlib-env docs --list` is a topic name under `docs/`. To spot-check one topic, substitute its name:

```bash
.wheel-venv-ci/bin/qatlib-env docs quickstart | cmp docs/quickstart.md -
```

`cmp` again prints nothing. The site build is not part of `ci.yml`. When Pages deploys is described in [GitHub workflows](docs/en/github-ci.md).

Without `uv` on the machine, build and test inside a separate virtual environment. This set does not include `uv lock --check` or the `uv venv` smoke test above, so it is not CI. Run the previous set once `uv` is available:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . build pytest hatchling jsonschema
.venv/bin/python -m build
.venv/bin/python -m pytest -q -rs --ignore=tests/migrated_dev/test_migrated_dev_smoke_packages.py
```

Build before the tests, so the wheel under `dist/` that the tests read comes from this worktree.

The public hygiene check recognizes common credentials and personal absolute paths. Extra denied terms go in `QATLIB_HYGIENE_DENYLIST`, one per line, compared without case.

Opening a draft, writing the verification record, and comparing the current head are in [Pull requests](docs/en/pull-requests.md).
