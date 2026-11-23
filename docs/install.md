# 安装方式

第一次验证请走[五分钟验证](quickstart.md)：在本工具仓的检出根目录执行 `uv tool install .`。这一页收录其余安装方式。无论哪一种，都使用隔离环境。不要对系统 Python 执行裸 `pip install`。

本仓库要求 Python 3.11 或更新版本。GitHub CI 使用 Python 3.12。

## 下游 Lean 仓库锁定的版本

在某个 Lean 仓库里做日常开发或跑该仓库的检查时，安装**那个仓库锁定的工具版本**，这样本地和 CI 用的是同一次发布。锁定声明如果写在该仓库的 `python/pyproject.toml` 里，就在该仓库根目录创建虚拟环境并安装 `./python`。目录布局不同的仓库，按其自己的说明安装。

使用 `uv`：

```bash
uv venv .venv
uv pip install --python .venv/bin/python ./python
source .venv/bin/activate
qatlib-env --help
qatlib-retrieve --help
```

使用标准库 `venv` 和 `pip`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install ./python
source .venv/bin/activate
qatlib-env --help
qatlib-retrieve --help
```

当前目录是下游仓库的根目录。成功时，`command -v qatlib-env` 指向刚激活的 `.venv`。这个虚拟环境只属于该仓库的这次检出。

## 脱离具体项目、直接调用命令

希望在任意目录调用三个命令时，用下面的某一种方式安装本工具仓。把 `/path/to/qatlib-tools` 换成检出路径、wheel 文件，或写在下一节里的固定提交 URL。

`uv`：

```bash
uv tool install /path/to/qatlib-tools
```

`uv` 自己维护工具环境。若找不到命令，运行 `uv tool dir --bin` 查看目录，再运行 `uv tool update-shell`，然后打开新的终端。

`pipx`：

```bash
pipx install /path/to/qatlib-tools
```

若命令目录不在 `PATH` 里，运行 `pipx ensurepath`，然后打开新的终端。

标准库 `venv`：把该环境的 `bin` 目录加入你平时使用的 shell 的用户 `PATH`。

```bash
python3 -m venv ~/.local/share/qatlib-tools-venv
~/.local/share/qatlib-tools-venv/bin/python -m pip install --upgrade pip
~/.local/share/qatlib-tools-venv/bin/python -m pip install /path/to/qatlib-tools
export PATH="$HOME/.local/share/qatlib-tools-venv/bin:$PATH"
```

成功时，`qatlib-env --help`、`qatlib-retrieve --help` 和 `qatlib-forge --help` 都能运行，并且 `command -v` 指向这个专用环境，而不是系统解释器。

## 固定某一次提交

要复现某一个已知修订、而手头又没有对应检出时，把 40 位提交 SHA 写进 URL。SHA 来自你核对过的提交，不要省略。

```bash
uv tool install 'git+https://github.com/quantum-atlas-ai/qatlib-tools.git@<提交 SHA>'
```

`<提交 SHA>` 替换成那 40 个十六进制字符。安装完成后，用 `qatlib-env docs --path` 确认读到的手册来自这个环境；需要逐字节对照时，见[贡献指南](../CONTRIBUTING.md#self-test)里的 `cmp` 步骤。

未写 SHA 的 `git+https://github.com/quantum-atlas-ai/qatlib-tools.git` 会跟随默认分支的当前尖端。五分钟验证和网站首页的示例都使用本地检出的 `uv tool install .`，这样装上的就是你正在看的这棵树。

## 从 wheel 安装

在本工具仓检出根目录构建，再把生成的 wheel 装进隔离环境：

```bash
uv build --wheel
uv tool install dist/qatlib_tools-*.whl
```

wheel 里的 `qatlib_env/_manual.md` 是构建当时的 `README.md` 字节，`qatlib_env/docs/` 是当时的 `docs/`。换一台没有源码树的机器时，可以只拷贝这个 wheel。核对字节的命令同样在[贡献指南](../CONTRIBUTING.md#self-test)。

## Windows

在 PowerShell 里激活虚拟环境用 `.venv\Scripts\Activate.ps1`。解释器路径写成 `.venv\Scripts\python.exe`，例如：

```powershell
uv pip install --python .venv\Scripts\python.exe ./python
.\.venv\Scripts\python.exe -m pip install ./python
```

专用虚拟环境的命令目录是 `Scripts`，不是 `bin`。缓存目录和工作树放在不同盘符上的行为、长路径，以及 `heavy_patterns = []`，见[Windows 跨卷和长路径](build.md#windows)。

第一次在 Lean 仓库里准备依赖或编译时，耗时见[五分钟验证](quickstart.md)末尾的说明。那一段不属于安装命令本身。
