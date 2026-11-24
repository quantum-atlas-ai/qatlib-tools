# Install Qatlib Tools

Have Git, Python 3.11+, and enough disk space for the Lean project. Use a virtual environment with an existing Python or install [uv](https://docs.astral.sh/uv/getting-started/installation/). Read the full Tools commit SHA from the downstream project's configuration or select a wheel with a verified SHA-256 from [Tools Releases](https://github.com/quantum-atlas-ai/qatlib-tools/releases).

## Linux

Run from the Lean project root after setting `TOOLS_SHA` to the full pinned SHA.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "git+https://github.com/quantum-atlas-ai/qatlib-tools.git@${TOOLS_SHA}"
.venv/bin/python -m pip check
.venv/bin/qatlib-env --version
.venv/bin/qatlib-env install-toolchain
.venv/bin/qatlib-env prepare
```

Install [Elan from its official releases](https://github.com/leanprover/elan/releases) before running `install-toolchain`. This command reads `lean-toolchain`, installs from the official source, and verifies Lean, Lake, and the sysroot. Check a representative project module with existing artifacts using `.venv/bin/qatlib-env run -- lake --no-build lean File.lean`.

## Windows PowerShell

Use [Git for Windows](https://gitforwindows.org/), Python 3.11+, and official Elan. Run from the project root after setting `$ToolsSha`.

```powershell
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "git+https://github.com/quantum-atlas-ai/qatlib-tools.git@$ToolsSha"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\qatlib-env.exe --version
.\.venv\Scripts\qatlib-env.exe install-toolchain
.\.venv\Scripts\qatlib-env.exe prepare --heavy-patterns=
.\.venv\Scripts\qatlib-env.exe run --heavy-patterns= -- lake --no-build lean File.lean
```

You can invoke the virtual environment executables directly. Managed builds run in a Windows Job Object. For a persistent setting, use `heavy_patterns = []` in your local configuration and confirm it with `qatlib-env config explain --json`.

Use a short path such as `D:\lean\project`. For the first checkout, run `git -c core.longpaths=true clone <URL> D:\lean\project`. If Git has downloaded all objects and the checkout failed solely on path length, confirm there are no edits you need to preserve and finish with `git -c core.longpaths=true checkout --force HEAD`. This option only applies to Git. The system's `LongPathsEnabled` also requires the application to declare `longPathAware`. Run `prepare` in each worktree. Across volumes, Lake copies artifacts that it cannot hard-link.

Use UTF-8 for scripts and manuals. In Windows PowerShell 5.1, `[IO.File]::WriteAllText(path, text, [Text.UTF8Encoding]::new($false))` writes UTF-8 without a BOM. Check LF, CRLF, and `.gitattributes` with `git ls-files --eol`. Lean 4.35 and Mathlib normalize CRLF to LF for source cache inputs. Windows `resolve-deps` sets `core.autocrlf=input` for its Git children and respects explicit dependency attributes. See [Windows builds and caches](build.md#windows).

## Optional public Lean Git mirror

`qatlib-env prepare` follows the original URLs by default. If you need a mirror, choose an HTTPS service you trust.

```bash
qatlib-env prepare --gh-proxy https://ghfast.top/
qatlib-env prepare --no-gh-proxy
```

The flags also work in PowerShell. Precedence is the CLI flag, `QATLIB_GITHUB_PREFIX`, the compatibility alias `QATLAS_GITHUB_PREFIX`, then direct. An empty canonical variable disables the alias. The prefix must end in `/` and contain no credentials, query, or fragment. The mirror receives only allowlisted full public Lean repository URLs and the locked commit. Tools tries verified local objects first. For missing objects, an isolated anonymous Git process fetches the pinned commit so Lake can read it locally. Ordinary Git, unknown URLs, private repositories, Gitea, R2, and APIs keep the caller's configuration. Floating updates use the original URLs. If the mirror returns 403 or 404 or times out, retry against the origin with `--no-gh-proxy`. For artifact restoration errors, inspect the Mathlib or R2 endpoint for the failing phase.

## Wheels and verified toolchain archives

Obtain the wheel SHA-256 from the official Release or a trusted manifest. Compare it with `sha256sum` or `Get-FileHash -Algorithm SHA256`, then run `-m pip install <wheel>` with the virtual environment's Python. To build from source, run `uv build`. If the downstream repository ships a Python package, follow its instructions for installing `./python`.

Obtain the official asset SHA-256 for your platform from [Lean Releases](https://github.com/leanprover/lean4/releases). Use the official ZIP on Windows or `.tar.zst` with `zstd` on Linux. The archive can arrive through a trusted artifact store.

```bash
qatlib-env install-toolchain --archive /path/to/lean-release.tar.zst --sha256 "$OFFICIAL_SHA256"
```

The command verifies the digest before extraction, checks the versions, and installs into Elan's exact release directory. Obtain the digest independently of any archive mirror. Tools' Git mirror flag applies only to the public Lean Git step. If Elan's download fails, record its stage and HTTP status and import the verified official archive. Use `qatlib-env docs install` to read the handbook packaged with the wheel.

<a id="resource-footprint"></a>

## Resource usage and cross-volume layout

On Windows, you can keep your user profile and shared caches on `C:` while placing the primary checkout and additional worktrees on `D:`. On Linux and macOS, you can likewise keep shared caches under `$HOME` and place worktrees on a larger data volume. Each worktree maintains its own `.lake` directory while sharing the global toolchain and caches. When artifacts are restored across volumes, Lake falls back to copying files, which consumes disk space on the destination volume; see [Windows cross-volume setups and long paths](build.md#windows) for details.

The table below reports measured resource usage on Windows with Lean `v4.35.0` across initial dependency preparation, a targeted module build, and spot checks in two worktrees. Actual usage varies with your dependency graph, cache hit rate, and build concurrency:

| Scope & Location | Component | Measured Footprint |
| --- | --- | ---: |
| Shared (`C:` / home directory by default) | Lean toolchain under `~/.elan` | ~3.26 GiB |
| Shared | R2/Lake cached artifacts under `~/.cache/lake` | ~0.90 GiB |
| Shared | Mathlib `.ltar` archives under `~/.cache/mathlib` | ~0.43 GiB |
| Shared | `uv` and Python runtime | ~0.52 GiB |
| **Shared total** | Net volume growth after initial preparation | **~5.1–5.6 GiB** |
| Primary checkout (can live on `D:`) | Primary checkout with `.git` (0.52 GiB), `.venv`, and unpacked `.lake/packages` (7.10 GiB) | ~7.6 GiB |
| Per additional worktree (can live on data volume) | Source checkout with shallow dependencies, vs. fully unpacked dependency artifacts | ~3.6 GiB (~10.0 GiB unpacked) |
| Memory (single process) | Peak single-process working set during dependency preparation, targeted build, and two-worktree checks | ~0.45–0.54 GiB |
| Memory (build process group) | Peak combined working set across build-related processes during the same run | ~1.00–1.29 GiB |

Note: Per-directory figures use logical file sizes, whereas the shared total reflects net volume usage. Because combined working sets sum sampled per-process measurements, shared memory pages may be counted more than once. When adding worktrees, budget disk space according to how much of the dependency cache each worktree will unpack. For parallel build sizing and admission control, see [Multiple Worktrees and Concurrency Budgets](build.md#note-concurrency).
