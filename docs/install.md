# 安装 Qatlib Tools

准备 Git、Python 3.11+ 和项目需要的磁盘空间。安装使用现有 Python 的虚拟环境，也可以使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)。从下游仓的配置取得完整 Tools 提交 SHA，或从 [Tools Releases](https://github.com/quantum-atlas-ai/qatlib-tools/releases) 取得已核验 SHA-256 的 wheel。

## Linux

在 Lean 项目根目录执行，先把 `TOOLS_SHA` 设为该项目锁定的完整 SHA。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "git+https://github.com/quantum-atlas-ai/qatlib-tools.git@${TOOLS_SHA}"
.venv/bin/python -m pip check
.venv/bin/qatlib-env --version
.venv/bin/qatlib-env install-toolchain
.venv/bin/qatlib-env prepare
```

`install-toolchain` 需要 [官方 Elan](https://github.com/leanprover/elan/releases)。命令读取 `lean-toolchain`，从官方源安装并核对 Lean、Lake 和 sysroot。最后用 `.venv/bin/qatlib-env run -- lake --no-build lean File.lean` 检查项目中已有工件的代表性模块。

## Windows PowerShell

使用 [Git for Windows](https://gitforwindows.org/)、Python 3.11+ 和官方 Elan。在项目根目录执行，并先设置 `$ToolsSha`。

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

可直接调用虚拟环境中的可执行文件。受管构建使用 Windows Job Object。长期设置可在个人配置中写 `heavy_patterns = []`，并通过 `qatlib-env config explain --json` 核对。

优先用短路径（如 `D:\lean\project`）。首次检出可用 `git -c core.longpaths=true clone <URL> D:\lean\project`。若已经下载 Git 对象且仅因长路径中断检出，在确认没有自己的改动后，用 `git -c core.longpaths=true checkout --force HEAD` 补齐。Git 开关只覆盖 Git 操作。系统的 `LongPathsEnabled` 还需要程序声明 `longPathAware`。每棵工作树单独运行 `prepare`，跨盘恢复时 Lake 会将无法硬链接的工件复制到目标卷。

脚本和手册使用 UTF-8。Windows PowerShell 5.1 生成文本时，可用 `[IO.File]::WriteAllText(path, text, [Text.UTF8Encoding]::new($false))` 显式写入无 BOM 的 UTF-8。用 `git ls-files --eol` 检查 LF/CRLF 和 `.gitattributes`。Lean 4.35 与 Mathlib 的源码缓存输入会将 CRLF 归一为 LF。Windows 上的 `resolve-deps` 对 Git 子进程设置 `core.autocrlf=input`，各依赖的显式属性仍生效。详见 [Windows 构建和缓存说明](build.md#windows)。

## 可选的公开 Lean Git 镜像

直接运行 `qatlib-env prepare` 使用原地址。仅在需要时启用 HTTPS 镜像，并选择信任的服务。

```bash
qatlib-env prepare --gh-proxy https://ghfast.top/
qatlib-env prepare --no-gh-proxy
```

PowerShell 使用相同参数。优先级为命令行、`QATLIB_GITHUB_PREFIX`、兼容变量 `QATLAS_GITHUB_PREFIX`、原地址。空的规范变量关闭兼容变量。URL 须以 `/` 结尾且无认证信息、查询参数或片段。镜像仅接收已登记的完整公开 Lean 仓 URL 和锁定提交。Tools 先查本地已验证对象，缺失时通过隔离的匿名 Git 子进程获取，Lake 从本地读取锁定对象。普通 Git、未知 URL、私有仓、Gitea、R2 和 API 沿用调用方配置。浮动更新使用原地址。镜像报 403、404 或超时时，可用 `--no-gh-proxy` 重试原地址。缓存恢复阶段的 HTTP 错误按 Mathlib 或 R2 端点排查。

## Wheel 与已验证工具链归档

从官方 Release 或可信清单取得 wheel 的 SHA-256，与 `sha256sum` 或 `Get-FileHash -Algorithm SHA256` 的结果核对，再用虚拟环境的 Python 运行 `-m pip install <wheel>`。构建者可从源码执行 `uv build`。如果下游有独立 Python 包，按该仓说明安装其 `./python`。

从 [Lean Releases](https://github.com/leanprover/lean4/releases) 取得对应平台归档的官方 SHA-256。Windows 可用官方 ZIP，Linux 可用官方 `.tar.zst`（需要 `zstd`）。归档可以通过可信内部存储传入。

```bash
qatlib-env install-toolchain --archive /path/to/lean-release.tar.zst --sha256 "$OFFICIAL_SHA256"
```

命令在解包前校验摘要，核对版本后安装到 Elan 的精确版本目录。镜像的下载结果与摘要需要独立核验。Tools 的 Git 镜像开关不参与 Elan、Lean 或 Python 安装。若 Elan 下载失败，保留阶段和 HTTP 状态，再按官方摘要导入归档。用 `qatlib-env docs install` 读取随 wheel 提供的手册。

<a id="resource-footprint"></a>

## 资源占用与跨盘布局

在 Windows 上，可以将用户主目录与共享缓存留在 `C:` 盘，将主检出目录和后续新增的工作树放在 `D:` 盘；在 Linux 与 macOS 上，同样可以将共享缓存保留在 `$HOME` 默认路径下，再把工作树放到容量充裕的数据盘。每棵工作树各自维护独立的 `.lake` 目录，同时共同复用全局工具链与共享缓存。需要注意，跨盘恢复编译工件时会回退为文件复制并占用目标盘空间，具体机制见[Windows 跨卷和长路径](build.md#windows)。

下表列出了基于 Lean `v4.35.0` 在 Windows 上完成依赖准备、少量模块编译及双工作树抽检时的实测资源量级（实际开销会随依赖规模、缓存命中率与构建并行度变化）：

| 资源范围与位置 | 统计项 | 实测占用 |
| --- | --- | ---: |
| 共享层（默认位于 C: / 用户主目录） | `~/.elan` 下的 Lean 工具链 | 约 3.26 GiB |
| 共享层 | `~/.cache/lake` 下的 R2/Lake 工件缓存 | 约 0.90 GiB |
| 共享层 | `~/.cache/mathlib` 下的 `.ltar` 压缩归档 | 约 0.43 GiB |
| 共享层 | `uv` 与 Python 运行时环境 | 约 0.52 GiB |
| **共享层合计** | 首次完成环境准备后的卷空间净增量 | **约 5.1–5.6 GiB** |
| 主检出目录（可置于 D: 等数据盘） | 单棵主检出，含 `.git`（0.52 GiB）、`.venv` 及解包后的 `.lake/packages`（7.10 GiB） | 约 7.6 GiB |
| 每棵新增工作树（可置于数据盘） | 源码与浅层依赖检出；若完整解包全部依赖工件则更高 | 约 3.6 GiB（完整解包约 10.0 GiB） |
| 内存峰值（单进程） | 依赖准备、少量模块编译与双工作树抽检期间的单进程工作集峰值 | 约 0.45–0.54 GiB |
| 内存峰值（编译进程组） | 同一轮操作中全部编译相关进程的工作集合计峰值 | 约 1.00–1.29 GiB |

注：表中各分项目录按文件逻辑大小统计，“共享层合计”取自磁盘卷实际已用空间的净增量；编译进程组的工作集合计由采样值相加得到，可能重复计入进程间共享的内存页。新增工作树时，请按该树实际需要展开的依赖规模预留磁盘空间；并行编译的配额换算与准入控制见[多工作树与并行预算](build.md#note-concurrency)。
