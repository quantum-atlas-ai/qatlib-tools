# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
· [English](README.en.md)

`qatlib-tools` 为 Lean 数学库及其下游研究仓库提供共用的工程手册与命令行工具；至于具体的数学接口设计、书目内容与合入标准，仍由各项目自行维护。

在日常协作中，开发者可以直接使用 `git`、GitHub 的 `gh`、Gitea 的 `tea` 或 `gt`，也可以直接调用代码托管平台 API；而多个仓库都需要处理的跨平台差异屏蔽、CI 检查结果汇总，以及执行写操作前的预期提交校验，则统一交由公共工具收敛处理。

## 命令

| 命令 | 用途 |
| --- | --- |
| `qatlib-env` | 准备 Lean 依赖与共享缓存、在资源配额下受控编译，以及管理本地工作树 |
| `qatlib-retrieve` | 检索 Lean 声明、源码签名与书目引用 |
| `qatlib-forge` | 汇总核对 GitHub 与 Gitea 的 PR 检查结果，并在写操作前校验预期提交 |

安装完成后，可随时运行 `qatlib-env docs` 离线阅读本手册；也可通过 `qatlib-env docs --list` 列出所有内置专题，再用 `qatlib-env docs <专题>` 查阅对应章节。各命令的具体参数以当前已安装版本的 `--help` 输出为准。

## 工程机制手册导览

| 专题 | 离线命令 | 内容 |
| --- | --- | --- |
| [编译环境、缓存与资源控制](docs/build.md) | `qatlib-env docs build` | Lake 配置、模块可见性、共享缓存、Windows 跨盘处理与本机资源预算 |
| [持续集成 Runner 与进程排障](docs/runner.md) | `qatlib-env docs runner` | cgroup 内存计费、取消任务时的进程回收与部署验收 |
| [Lean 文档、书目与源码检索](docs/lean-docs.md) | `qatlib-env docs lean-docs` | doc-gen4 跨页链接、增量书目校验与静态词法索引 |
| [GitHub 和 Gitea 上的拉取请求](docs/forge.md) | `qatlib-env docs forge` | 两种草稿机制、CI 状态核对、并发安全合入与分支清理 |
| [共享成果如何进入公开库](docs/promotion.md) | `qatlib-env docs promotion` | 整文件依赖、消费者验证、草稿演练与发布后切换依赖 |

<a id="quickstart"></a>

## 安装

一次安装即可同时提供 `qatlib-env`、`qatlib-retrieve` 与 `qatlib-forge` 三个命令行入口。请根据实际使用场景选择对应的安装方式，并全程在隔离的虚拟环境中操作，切勿直接对系统 Python 解释器执行裸 `pip install`。

### 项目锁定版本

在具体仓库中开展日常开发或运行项目检查时，应当使用该项目锁定的工具版本，以保持与 CI 环境一致。如果项目将锁定依赖声明在 `python/pyproject.toml` 中，可在项目根目录下新建虚拟环境并直接安装 `./python`；采用其他目录布局的仓库，请遵循其自身的安装说明。

- **使用 `uv`**

  ```bash
  uv venv .venv
  uv pip install --python .venv/bin/python ./python
  source .venv/bin/activate
  qatlib-env --help
  qatlib-retrieve --help
  ```

- **使用 `venv` + `pip`**

  ```bash
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install ./python
  source .venv/bin/activate
  qatlib-env --help
  qatlib-retrieve --help
  ```

在 Windows PowerShell 下，请改用 `.venv\Scripts\Activate.ps1` 激活虚拟环境，并将安装命令中的解释器路径替换为 `.venv\Scripts\python.exe`（例如运行 `uv pip install --python .venv\Scripts\python.exe ./python` 或 `.venv\Scripts\python.exe -m pip install ./python`）。若需单独测试某个特定版本的工具，可在另一处隔离环境中安装由该提交构建出的 wheel 包，并对照构建记录核实其对应的源码提交。

### 独立命令行安装

若希望脱离具体项目的虚拟环境、在任意目录下直接调用 `qatlib-env`、`qatlib-retrieve` 与 `qatlib-forge`，可将下列命令中的 `/path/to/qatlib-tools` 按需替换为本地检出路径、已下载的 `.whl` 文件，或可访问仓库的固定提交 `'git+https://github.com/quantum-atlas-ai/qatlib-tools.git@<提交 SHA>'`：

- **使用 `uv`**：运行 `uv tool install /path/to/qatlib-tools`，由 `uv` 自动维护独立的工具环境；若终端提示找不到命令，可先用 `uv tool dir --bin` 查看命令所在目录，再运行 `uv tool update-shell` 将其加入 `PATH`。
- **使用 `pipx`**：运行 `pipx install /path/to/qatlib-tools`；若命令目录尚未加入 `PATH`，可执行 `pipx ensurepath` 补齐。
- **使用 `venv` + `pip`**：手动创建专用虚拟环境，并将其 `bin` 目录（Windows 下为对应的 `Scripts` 目录）加入日常所用 shell 的用户 `PATH`：

  ```bash
  python3 -m venv ~/.local/share/qatlib-tools-venv
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install --upgrade pip
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install /path/to/qatlib-tools
  export PATH="$HOME/.local/share/qatlib-tools-venv/bin:$PATH"
  ```

<a id="configuration"></a>

## 构建资源

在未显式设置资源预算时，`qatlib-env` 会综合宿主机的可用内存、进程 CPU 亲和性以及当前可见的 cgroup v1/v2 限额，自动推算默认资源池容量；其中核算宿主机可用内存余量时，会预留扣除 2 GiB 与物理内存总量 10% 二者中的较大值作为系统缓冲。完全采用默认配置时，单次构建通常申请资源池约一半的容量，且默认上限不超过 8 个 CPU、16 GiB 内存和 4 个并行编译进程；若资源池本身较小，单次构建可使用其全部余量，而在单核环境下则默认退化为 1 个编译进程配 1 个 Lean 线程。

如需查看预算推导详情，可运行只读诊断命令 `qatlib-env config explain --json`：该命令不会拉取依赖或申请构建池配额，而是直接展示探测到的底层容量（`resources`）、无覆盖时的自动推导值（`automatic`），以及最终生效的参数与路由配置（`values` 和 `configuration.routing`），便于结合来源记录（`sources`、`configuration.sources`）与告警提示（`warnings`）判断是否需要手动调整。需要长期保留的本机设置可写入 `~/.qatlib/env.toml`，单次构建的临时调整则可通过相应的命令行参数传入。工具会保留用户显式配置的值，但作业实际运行时仍受共享构建池余量与底层平台资源限额的共同约束。详见[配置来源与资源探测](docs/build.md#configuration)。

## 本机目录

工具自身的默认状态根目录为 `~/.qatlib`，可通过环境变量 `QATLIB_HOME` 重定向至其他位置。在该目录下，个人配置文件为 `env.toml`，本地检索索引存放在 `retrieve/`，新建构建池账本与云端作业回执则分别位于 `state/pool/` 和 `state/cloud/`；如需核对各项数据的实际读写位置，可查看 `qatlib-env config explain --json` 输出中的 `paths` 字段。版本升级后，工具仍能兼容读取既有的配置文件与索引，运行中的构建池也会继续沿用原有的账本与锁文件；至于 Lake 编译工件、上游 CLI 的登录配置以及历史工作树池，则各自维护在独立的路径中。详见[目录与兼容规则](docs/build.md#local-paths)。

<a id="artifact-cache"></a>

## 编译与共享工件缓存

```bash
qatlib-env prepare
qatlib-env plan MyLib.Facts
qatlib-env run -- lake build MyLib.Facts
```

执行编译时，Lake 会沿着导入关系依次构建目标模块及其上游依赖。若上游依赖工件已经就绪且哈希匹配，可通过 `qatlib-env run -- lake --no-build lean <File.lean>` 快速检查单个源码文件；一旦遇到缺少待构建依赖的情况，命令便会直接报错。

在启用 `module` 机制的 Lean v4.35 源码中，模块内的声明默认为私有：需要置于 `public section` 内或显式添加 `public` 修饰，下游模块才能引用其名称；若还需向更下游转发被导入模块的公开接口，则应使用 `public import`。如果下游除了引用名称之外，还需要在证明或计算中展开普通定义体，则须进一步核对声明是否标有 `@[expose]`，并编写确实依赖定义展开的用例加以验证。需要注意，未启用 `module` 的普通文件仍能读取模块内部的私有信息，若仅用此类文件测试，往往无法提前暴露真正的 `module` 消费者拿不到声明名称或无法展开定义体的问题。

在日常开发与依赖拉取中，三类预编译工件按不同机制存放与复用：

1. **Lake 本地共享工件**：直接调用 Lean v4.35 的 Lake 且未显式设置缓存选项时，Lake 默认只读共享缓存，但命中模块后仍会将工件恢复到当前包的构建目录中。多工作树并发开发时，建议设置 `enableArtifactCache = true` 与 `restoreAllArtifacts = false`：一旦命中共享目录中的原生工件，Lake 会依据输入跟踪信息定位内容哈希，直接原地读取 `LAKE_CACHE_DIR` 下的 `.olean`，而无需文件系统支持 reflink 或建立跨工作树硬链接；同时，各工作树仍保有各自独立的 `.lake` 目录，供编辑器使用的 `.ilean` 索引文件也照常在本地落盘。
2. **R2 远端工件**：对于配置了 R2 远端缓存的依赖包，`qatlib-env prepare` 会先按依赖锁备齐源码，接着下载并校验本地缺失的预编译工件，再将映射关系注册到 Lake 本地缓存中。由于发布单个工件时所需的硬链接操作完全在缓存目录内部完成，因此在 Windows 上可以让共享缓存与工作树分别存放在不同盘符上。
3. **Mathlib 专用缓存**：在标准依赖布局下，Mathlib 的 `lake exe cache get` 会将预编译归档直接解包到当前工作树的 `.lake/packages/mathlib/.lake/build/` 目录中。

用 `qatlib-env workspace scan <目录> [其他目录 ...]` 查找工作树，可加 `--inspect-ignored` 检查 Git 忽略的文件。

关于缓存字段的默认值、根包与依赖包之间的配置覆盖次序，以及恢复或下载工件遇到跨盘场景时的具体行为，请参阅[Lake 缓存开关](docs/build.md#cache-flags)与[Windows 跨盘与路径长度](docs/build.md#windows)。

如需回收闲置工作树占用的磁盘空间，可先运行 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览待清理的 `.lake/build` 与 `.lake/packages` 目录；若准备回收整个关联工作树，可先执行 `qatlib-env workspace trash-checkouts --checkout <路径>` 预览影响范围，确认无误后再改用 `--apply` 将其移入回收站。工作树成功移入回收站后，工具默认会一并清理所属 Git 仓库中残留的失效工作树登记。详见[工作树与工件存储](docs/build.md#cache-layout)。

<a id="retrieval"></a>

## Lean 文档与检索

运行 `qatlib-retrieve index` 可在本地构建静态词法索引，索引库默认写入 `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`，其中 `<corpus-key>` 取项目根目录解析后绝对路径的 SHA-256 前 24 个十六进制字符。若通过 `--db` 另选数据库文件，目标路径必须位于所有纳入索引的源码仓库之外。索引就绪后，查询命令可直接定位目标声明及其外层 `section` 中的 `variable` 声明，全程无需启动 Lean 编译器或 Lake：

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

编写源码注释时，使用模块级注释 `/-! ... -/` 说明当前模块的整体职责，并用紧贴声明上方的 `/-- ... -/` 文档串记录数学语义、前提条件与典型用法。文内跨声明引用请用反引号包裹完整限定名，文献引用则用 `[CiteKey]` 对应 `docs/references.bib` 中的条目。下游开发仓可仅在本地维护新增书目，执行书目校验时工具会自动联合锁定的公开基线，统一排查缺键、重复键与同键异文等问题。

在文档渲染方面，`doc-gen4` 生成整站文档时具备全局符号表、页面路由与书目索引，能够将反引号声明名和书目键自动转换为跨页链接；而编辑器的 Lean LSP hover 仅渲染当前声明自身的文档片段，没有整站路由和全局书目索引。因此，若希望在编辑器 hover 浮窗中也能直接打开参考论文，可在注释中补充指向 DOI 或 arXiv 的普通 Markdown 链接。详见[Lean 文档、书目与源码检索](docs/lean-docs.md)。

<a id="forge"></a>

## GitHub 和 Gitea

日常查看、创建和评论 PR 可直接使用 `gh`、`tea` 或 `gt`。当需要跨多个接口汇总检查结果，或附带预期提交校验执行受保护的写操作时，可使用下列命令：

<a id="cli-reference"></a>

| 命令 | 用途 |
| --- | --- |
| `qatlib-forge pr checks` | 汇总同一 PR head 上的相关检查与实际作业状态 |
| `qatlib-forge pr paths` | 核对构建路径匹配情况与复用既有 CI 结果的条件 |
| `qatlib-forge pr activity` | 过滤可识别的机器人活动，查看长期冷置的候选 PR |
| `qatlib-forge pr merge` | 附带预期 head 校验执行 PR 合并 |
| `qatlib-forge branch delete` | 核对远端 SHA 与分支保护规则后安全删除分支 |

GitHub 与 Gitea 的草稿表示和状态接口各不相同：GitHub 提供独立的 `isDraft` 字段，并将检查信息分别暴露在 check-runs、提交 statuses 和 Actions 作业接口中；Gitea 的草稿状态则跟随实例配置的标题前缀（如 `WIP:` 或 `[WIP]`）。在草稿 PR 中，Build 作业可能已按条件跳过，但外层提交状态摘要仍可能显示为 `pending`。因此核查状态时，必须按同一个 PR head SHA 穿透查询工作流运行记录与具体作业，准确区分条件跳过（`skipped`）、尚未运行与真正执行成功。

当多位贡献者与智能体并发提交草稿时，一轮全库编译往往耗时数十分钟，而静态文档检查通常只需约一分钟。若 PR 仅涉及未命中构建路径的文档改动，可按仓库规则仅运行静态检查；在此基础上，只要受测 PR head 未发生变化，主线在此期间新增的提交同样是不命中构建规则的文档改动，且与当前 PR 的修改范围互不相交，便可直接复用该 head 上已验证成功的作业结果，省去一轮重复编译。不过在复用前，路径规则、受测基线与既有作业证据仍须逐项核对无误。

在人工审阅完毕到实际执行合并或删分支之间，远端分支上仍可能进入新的推送。为此，`qatlib-forge pr merge` 会把预期的 head 提交一并交给服务端核对；`qatlib-forge branch delete` 则会先检查目标是否为默认分支或命中保护规则，再借助 Git 的 `--force-with-lease` 要求远端引用仍指向预期 SHA，防止误删新提交。详见[GitHub 和 Gitea 上的拉取请求](docs/forge.md)。

<a id="promotion"></a>

## 共享成果进入公开库

跨仓共享的形式化成果按三层链路推进：先在孵化仓内整理可复用结论，再送入预发仓核验整文件依赖闭环与真实消费者兼容性，最后由公开库发布稳定接口与预编译工件。预发验证通过后，下游仓库即可提前开启草稿 PR 演练接口切换；待公开库的正式版本与远端工件就绪后，再在同一张 PR 中更新依赖锁，并同步清理已被替代的旧源码及已收录进公开基线的增量书目。至于各仓库具体的目录规划、评审流程与收录标准，由各仓自身的晋升说明负责定义。详见[晋升步骤与临时别名检查](docs/promotion.md)。

<a id="field-notes"></a><a id="runner"></a>

## 构建排障与 Runner

在 Linux cgroup v2 中，`memory.current` 会同时计入进程匿名内存与文件页缓存（`file`）。当大批量编译工件读写结束后，即便编译进程本身已经退出，尚未回收的页缓存仍可能继续占用该控制组的账面内存额度。因此排查构建池准入等待时，应当同时查看 `memory.current`、`memory.stat` 中的细分统计以及各级控制组的内存限制。

常驻运行的 CI Runner 在取消单次作业时，通常只会终止该次作业的进程，而不会停止整个 Runner 服务，因而不会触发服务层面的 `KillMode=control-group`。在 Linux 上，工具通过 `PR_SET_PDEATHSIG` 让守卫进程在父进程退出时及时收到终止信号，并利用 `PR_SET_CHILD_SUBREAPER` 收养失去原父进程的子孙进程以便彻底回收；在具备可用 systemd 后端的环境中，重型任务还可放入独立的瞬态 scope 运行，以便在取消任务时按控制组整组清扫残留进程。

配置并发参数时，`jobs` 表示并行编译进程数，`lean_threads` 表示单个进程内部的线程数。构建池按两者乘积核算总 CPU 配额申请量——例如 4 个进程各开 4 个线程，便会申请 16 个 CPU 配额。同一宿主机上的多个并行工作树应共用同一份构建池账本以协同限流，而各工作树自身的 `.lake` 目录仍保持彼此独立。详见[持续集成 Runner 与进程排障](docs/runner.md)。

<a id="self-test"></a>

## 工具仓自测与离线手册校验

维护本工具仓时，使用 Python 3.12 依次运行 `uv lock --check`、`uv build` 和 `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs`，即可在本地执行与 GitHub CI 完全一致的构建与全量测试。

若当前环境未安装 `uv`，也可在新建的虚拟环境内使用 `build` 生成分发包后再运行自测，并将直接调用 `uv` 的安装测试留待具备 `uv` 的环境复验。注意务必先完成构建再运行测试，以确保 `dist/` 目录中供测试读取的 wheel 包确实来自当前工作区源码：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . build pytest hatchling jsonschema
.venv/bin/python -m build
.venv/bin/python -m pytest -q -rs --ignore=tests/migrated_dev/test_migrated_dev_smoke_packages.py
```

构建出的 wheel 会将仓库根目录的 `README.md` 按原始字节打包为 `qatlib_env/_manual.md`，并将 `docs/` 打包到 `qatlib_env/docs/`。运行 `qatlib-env docs --list` 时，工具只列出 `docs/` 直接下属的 Markdown 专题，阅读时使用不含 `.md` 后缀的文件名选择。使用 `pipx` 或干净的 `venv` 安装当前 wheel 并确认所调命令来自刚安装的环境后，即可在仓库根目录分别执行 `qatlib-env docs | cmp README.md -` 与 `qatlib-env docs build | cmp docs/build.md -`，逐字节比对离线主手册与专题内容是否同源码完全一致。

本项目采用 [Apache License, Version 2.0](./LICENSE) 开源许可。
