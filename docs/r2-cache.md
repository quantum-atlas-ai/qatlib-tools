# 使用 Cloudflare R2 跨仓复用缓存

GitHub Actions 缓存以仓库为边界。同仓内能恢复哪些缓存，还取决于分支和触发事件。私有预发布仓库与独立的公开仓库即使用相同的键和 Runner，也无法交换 Actions 缓存。公开发布提交可以只以上一次公开提交为父提交，同时沿用私有生产端的构建输入。让两边用内容键在同一个 R2 命名空间查找产物，就无需共享 Git 历史。 [GitHub 缓存访问限制](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)

## 建桶并分开读写

1. 在 Cloudflare 的 **R2 Object Storage** 选择 **Create bucket**，创建例如 `lean-cache` 的桶，只放允许公开分发的工件。私有工件用独立桶。上传前检查 HTML、清单和归档是否带入未公开内容、私有源链接、本机路径或凭据。
2. 在桶的 **Settings → Custom Domains** 绑定托管在 Cloudflare 的域名，例如 `cache.example.com`。为不可变内容配置 CDN 缓存，为 `latest.json` 等可变别名设短有效期或绕过缓存。临时探测可用公开开发 URL。 [R2 公开桶](https://developers.cloudflare.com/r2/buckets/public-buckets/)
3. 在 **Manage API Tokens** 创建 **Object Read & Write** 凭据，范围限定到这个桶。将 Access Key ID 和 Secret Access Key 放在仅允许受保护生产分支使用的 GitHub Actions 环境中。S3 入口为 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`，Lake 的写入 URL 路径要带桶名。公开域名接受匿名 GET。 [R2 鉴权](https://developers.cloudflare.com/r2/api/tokens/)
4. 将下方配置提交为 `lake-cache.toml`。原生 Lake 使用其绝对路径作为 `LAKE_CONFIG`。只在受信上传步骤注入 `<access-key-id>:<secret-access-key>`，原生 Lake 使用 `LAKE_CACHE_KEY`，`qatlib-env` 使用包专用变量。PR 与公开 Pages 作业均不注入 R2 写凭据。Pages 部署权限单独配置。

```toml
cache.defaultService = "r2-read"
cache.defaultUploadService = "r2-write"

[[cache.service]]
name = "r2-read"
kind = "s3"
artifactEndpoint = "https://cache.example.com/artifacts"
revisionEndpoint = "https://cache.example.com/revisions"

[[cache.service]]
name = "r2-write"
kind = "s3"
artifactEndpoint = "https://ACCOUNT_ID.r2.cloudflarestorage.com/lean-cache/artifacts"
revisionEndpoint = "https://ACCOUNT_ID.r2.cloudflarestorage.com/lean-cache/revisions"
```

## 三层内容键

两端共同选择一个稳定的 `<repo>` 命名空间，如 `quantum-atlas-ai/qatlib`，并使用同工具链、兼容的 Runner 平台。下面的 `<tc>` 由工具链名转义得到，把 `/` 替换为 `--`，把 `:` 替换为 `---`。

| 层 | 对象键示例 | 复用条件 |
| --- | --- | --- |
| Lean 编译工件 | `revisions/<repo>/tc/<tc>/inputs/<INPUT_TREE>.jsonl` 与 `revisions/<repo>/tc/<tc>/<sha>.jsonl` | 将已提交构建输入的 Git 对象 ID 按顺序拼接，每个 ID 后添加 LF，再求 SHA-256。Qatlib 示例依次使用 `Qatlib`、`Qatlib.lean`、`lakefile.toml`、`lean-toolchain`、`lake-manifest.json`。若还有其他构建输入，生产端与消费端都应纳入。将 `.ltar` 作为 Lake 工件写入 `artifacts/<repo>/<hash>.art`，先发布工件，再以两种键写入完整清单。读取先查提交键，再查输入树键。祖先清单可作为增量编译的底座。 |
| 增量文档分析 | `docs/<repo>/tc/<tc>/analysis/<compatible>/latest.tar.gz` | 用格式版本、工具链、依赖锁与文档构建配置计算 `compatible`。成套恢复一致的 SQLite 备份及配套生成数据，由项目构建系统判定哪些模块需重算。为可变的 `latest` 别名指定唯一发布端或串行写入。 |
| 精确原始 HTML | `docs/<repo>/tc/<tc>/html/<fingerprint>.tar.gz` | 指纹纳入格式版本、生成设置和全部 HTML 生成输入的规范化 Git 索引条目（mode、对象 ID、路径）。由 Pages 单独组装的门户和 CHANGELOG 排除在外。安装 Lean 前先取归档并验完整性。命中时可直接组装部署，热路径示例约两分钟。 |

文档缓存格式由消费项目维护。先上传归档，再发布带格式和 SHA-256 的元数据。下载后核验元数据与归档，检查成员路径和类型，再解包到新目录。保留旧的不可变快照供回滚。明确的 `404` 可视作未命中。传输、鉴权和完整性错误应停止或显式重试。

## 原生 Lake 消费工作流

示例假定项目使用 TOML、提交依赖锁、启用了 `enableArtifactCache = true`，并有 `lake test` 驱动。将目标与有序输入路径同生产端一起调整。若项目有大型依赖，可另加依赖缓存准备步骤。运行只需 Git、curl 与项目锁定 Lean 工具链里的 Lake。 [Lake 缓存命令](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/)

```yaml
name: Lean with R2
on: [push, pull_request]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      LAKE_CONFIG: ${{ github.workspace }}/lake-cache.toml
      LAKE_ARTIFACT_CACHE: "true"
      CACHE_REPO: quantum-atlas-ai/qatlib
      REVISION_ENDPOINT: https://cache.example.com/revisions
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: leanprover/lean-action@v1
        with:
          auto-config: "false"
          build: "false"
          test: "false"
          lint: "false"
          use-mathlib-cache: "false"
          use-github-cache: "false"
      - name: Fetch revision or input-tree manifest
        shell: bash
        run: |
          tree=$(git rev-parse HEAD:Qatlib HEAD:Qatlib.lean HEAD:lakefile.toml \
            HEAD:lean-toolchain HEAD:lake-manifest.json | sha256sum | cut -d' ' -f1)
          tc=$(<lean-toolchain)
          tc=${tc//\//--}
          tc=${tc//:/---}
          base="$REVISION_ENDPOINT/$CACHE_REPO/tc/$tc"
          for key in "$(git rev-parse HEAD).jsonl" "inputs/$tree.jsonl"; do
            status=$(curl -q -sS --connect-timeout 15 --max-time 120 \
              -o "$RUNNER_TEMP/outputs.jsonl" -w '%{http_code}' "$base/$key")
            echo "manifest=$key http_status=$status"
            case "$status" in
              200) echo "manifest_hit=true" >> "$GITHUB_ENV"; break ;;
              404) ;;
              *) exit 1 ;;
            esac
          done
      - name: Restore and check the build
        shell: bash
        run: |
          if [[ "${manifest_hit:-}" == true ]]; then
            lake cache get "$RUNNER_TEMP/outputs.jsonl" --repo="$CACHE_REPO" --service=r2-read
            if lake --no-build build Qatlib; then
              echo "no_build=verified"
              exit 0
            fi
            echo "fallback=no-build-rejected"
          else
            echo "fallback=cache-miss"
          fi
          lake --fail-fast build Qatlib
      - name: Run the project audit
        run: lake test
```

## 使用 qatlib-env 的消费工作流

在 `qatlib-env.toml` 中设定仓库配置，让两段历史查找同一个命名空间。项目如有额外的测试构建目标，可设置 `tests`。两端提交同一份 Lake 读取配置。示例固定 CLI 到 v0.3.0，须等该版本公开后采用。专用 CI Runner 已授权在缓存未命中时编译，仍遵守资源上限。

```toml
library = "Qatlib"
cache_repository = "quantum-atlas-ai/qatlib"
```

```yaml
name: Managed Lean build
on: [push, pull_request]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@v5
      - uses: leanprover/lean-action@v1
        with:
          auto-config: "false"
          build: "false"
          test: "false"
          lint: "false"
          use-mathlib-cache: "false"
          use-github-cache: "false"
      - name: Install and build
        shell: bash
        run: |
          uv tool install 'qatlib-tools @ git+https://github.com/quantum-atlas-ai/qatlib-tools.git@v0.3.0'
          export PATH="$(uv tool dir --bin):$PATH"
          qatlib-env cache inspect --remote --json
          qatlib-env ci build --allow-local-heavy
```

独立的受信生产端可在受保护的 `main` 上执行托管构建，只向该作业注入包专用写凭据，并将 `defaultUploadService` 指向 S3 API。原生生产端可用 `lake cache stage` 和 `put-staged` 上传工件，再用签名的 S3 PUT 写入完整清单别名。先通过构建和审计门禁，再发布。

## 四步验证交接

1. **访问。** 用限定桶权限的身份上传无害探测对象。无凭据运行 `curl -q -fsS https://cache.example.com/probe.txt` 并比对返回字节。再向公开域名发送无凭据 PUT，请求应被拒绝。
2. **内容键。** 在 SHA 不同且输入相同的两份干净检出中运行 `qatlib-env cache inspect --json`，比较 `paths` 和 `input_tree`。可重复指定 `--path` 来明确路径顺序。文档兼容键和 HTML 指纹也用项目自身的脚本比较。
3. **Lean。** 在消费端运行 `qatlib-env cache inspect --remote --json`。新提交的别名可以是 `404`，输入树别名应返回 `200` 和正数 `entry_count`。恢复工件后验证 `lake --no-build build Qatlib`，再跑内核审计。受信生产端补写消费端 SHA 别名后，再检查该别名。
4. **Pages。** 检查精确归档是否在 Lean 初始化前成功恢复和验证。分别测试缺少对象、下载中断、摘要错误。缺少对象时可走无写凭据的本地生成路径。传输或校验失败应有明确报错。

`cache inspect` 对已提交的 `HEAD` 对象求哈希。默认路径由当前检出的配置决定，请使用相应的干净检出。`--remote` 用 curl 匿名 GET 两份清单并检查 Lake 映射结构。HTTP `404` 是有效的探测结果。传输、异常 HTTP 状态和清单格式错误会返回非零退出码。工件验证需要实际恢复并探测构建状态，见 [Actions 阶段计时与缓存回退排障](actions-timing.md)。
