# Install

For a first check, follow the [five-minute check](quickstart.md) and run `uv tool install .` from the root of a checkout of this tools repository. This page collects the other install methods. Every method uses an isolated environment. Do not run a bare `pip install` into the system Python.

This repository requires Python 3.11 or newer. GitHub CI uses Python 3.12.

## The version locked by a downstream Lean repository

For day-to-day work or for that repository's own checks, install **the tool version that Lean repository locks**, so the local environment and CI use the same release. When the lock is declared in that repository's `python/pyproject.toml`, create a virtual environment in the repository root and install `./python`. A repository with a different layout follows its own install notes.

With `uv`:

```bash
uv venv .venv
uv pip install --python .venv/bin/python ./python
source .venv/bin/activate
qatlib-env --help
qatlib-retrieve --help
```

With the standard-library `venv` and `pip`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install ./python
source .venv/bin/activate
qatlib-env --help
qatlib-retrieve --help
```

The working directory is the downstream repository root. On success, `command -v qatlib-env` points at the `.venv` you just activated. That environment belongs to this checkout of that repository.

## Calling the commands outside one project

To invoke the three commands from any directory, install this tools repository with one of the methods below. Replace `/path/to/qatlib-tools` with a checkout, a wheel file, or the pinned-commit URL in the next section.

`uv`:

```bash
uv tool install /path/to/qatlib-tools
```

`uv` maintains the tool environment. If the command is not found, run `uv tool dir --bin`, then `uv tool update-shell`, and open a new terminal.

`pipx`:

```bash
pipx install /path/to/qatlib-tools
```

If the command directory is not on `PATH`, run `pipx ensurepath` and open a new terminal.

Standard-library `venv`: add that environment's `bin` directory to the user `PATH` of the shell you actually use.

```bash
python3 -m venv ~/.local/share/qatlib-tools-venv
~/.local/share/qatlib-tools-venv/bin/python -m pip install --upgrade pip
~/.local/share/qatlib-tools-venv/bin/python -m pip install /path/to/qatlib-tools
export PATH="$HOME/.local/share/qatlib-tools-venv/bin:$PATH"
```

On success, `qatlib-env --help`, `qatlib-retrieve --help`, and `qatlib-forge --help` all run, and `command -v` points at this dedicated environment rather than the system interpreter.

## Pinning one commit

To reproduce a revision you have already identified, and you do not have that checkout at hand, put the 40-character commit SHA in the URL. Use the full SHA from the commit you checked.

```bash
uv tool install 'git+https://github.com/quantum-atlas-ai/qatlib-tools.git@<commit SHA>'
```

Replace `<commit SHA>` with those 40 hexadecimal characters. After installation, `qatlib-env docs --path` shows which manual this environment reads. The byte-for-byte `cmp` steps are in the [contributing guide](../../CONTRIBUTING.en.md#self-test).

A `git+https://github.com/quantum-atlas-ai/qatlib-tools.git` URL with no SHA follows the current tip of the default branch. The five-minute check and the site homepage both use `uv tool install .` on a local checkout, so the install matches the tree you are reading.

## Installing from a wheel

Build in the checkout root of this tools repository, then install the wheel into an isolated environment:

```bash
uv build --wheel
uv tool install dist/qatlib_tools-*.whl
```

Inside the wheel, `qatlib_env/_manual.md` is the bytes of `README.md` at build time, and `qatlib_env/docs/` is the `docs/` tree at build time. A machine without the source tree can receive just this wheel. The comparison commands are again in the [contributing guide](../../CONTRIBUTING.en.md#self-test).

## Windows

In PowerShell, activate a virtual environment with `.venv\Scripts\Activate.ps1`. Write the interpreter path as `.venv\Scripts\python.exe`, for example:

```powershell
uv pip install --python .venv\Scripts\python.exe ./python
.\.venv\Scripts\python.exe -m pip install ./python
```

The command directory of a dedicated virtual environment is `Scripts`, not `bin`. Behavior when the cache and the worktree are on different drive letters, long paths, and `heavy_patterns = []` are in [Windows cross-volume paths](build.md#windows).

The time spent the first time a Lean repository prepares dependencies or compiles is described at the end of the [five-minute check](quickstart.md). That wait is separate from the install command itself.
