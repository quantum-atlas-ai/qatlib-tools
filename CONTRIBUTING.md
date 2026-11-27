# 贡献指南

修改共用工具之前，先确认使用它的项目锁定了哪个版本，以及实际怎样调用它。每条命令只做一件事，方便和别的命令组合。各项目自己的规则和验收标准留在各项目里。

本仓库的设计原则和人工审阅约定在 [DESIGN.md](DESIGN.md)。受保护区块、起止标记和 `docs/design/human-contracts.sha256` 的变更先开草稿，按该文件的流程送审。

<a id="self-test"></a>

## 与 CI 相同的本地自测

当前目录是本工具仓的检出根目录。使用 Python 3.12。下面的顺序和 `.github/workflows/ci.yml` 里的 `test` 作业一致。草稿拉取请求在 CI 里会给契约脚本追加 `--draft`；准备把拉取请求标为就绪时，本地运行不带 `--draft` 的检查。

```bash
uv run --no-project python scripts/check_human_contracts.py
uv lock --check
uv build
uv run --locked python -m pytest -q -rs
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

成功时，每条命令退出码为 0。`cmp` 没有输出，表示装进干净环境的手册和当前 `README.md` 字节相同。`qatlib-env docs --list` 的每一行都是 `docs/` 下的一个专题名。抽查一篇时，把名字换进去：

```bash
.wheel-venv-ci/bin/qatlib-env docs quickstart | cmp docs/quickstart.md -
```

`cmp` 仍然没有输出。站点构建不在 `ci.yml` 里。Pages 何时部署见[GitHub 工作流](docs/github-ci.md)。

本机没有 `uv` 时，可以在单独的虚拟环境里先构建再测试。这组命令不包含 `uv lock --check`，也不包含上面的 `uv venv` 冒烟，所以它不等于 CI。具备 `uv` 之后要补跑上一组：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[literature]' build pytest hatchling
.venv/bin/python -m build
.venv/bin/python -m pytest -q -rs --ignore=tests/migrated_dev/test_migrated_dev_smoke_packages.py
```

先完成构建，再运行测试，这样测试读到的 `dist/` wheel 来自当前工作区。

公开内容检查会识别常见凭据和个人绝对路径。额外的禁用词通过 `QATLIB_HYGIENE_DENYLIST` 逐行传入，比较时忽略大小写。

开草稿、写验证记录和对照当前 head 的步骤在[拉取请求](docs/pull-requests.md)。
