# Cross-repository caching with Cloudflare R2

GitHub Actions caches are scoped to a repository. Within that repository, branch and event rules constrain which entries a run can restore. Even with the same key or runner, a cache saved in a private pre-release repository is unavailable to an independent public repository. A public release commit can have the previous public commit as its only parent while sharing the same build inputs as the private producer. Use those inputs and a shared R2 namespace to find the artifacts in both histories. [GitHub cache access restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)

## Create the bucket and split access

1. In Cloudflare, open **R2 Object Storage**, choose **Create bucket**, and create a bucket such as `lean-cache` for artifacts cleared for public distribution. Keep private artifacts in a separate bucket. Review generated HTML, manifests, and archives for unpublished content, private source links, local paths, and credentials before upload.
2. In the bucket's **Settings → Custom Domains**, connect a hostname in your Cloudflare account, such as `cache.example.com`. Configure caching for immutable objects. Give mutable aliases such as `latest.json` short lifetimes or bypass caching for them. The public development URL is suitable for a quick probe. [R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/)
3. In R2's **Manage API Tokens**, create an **Object Read & Write** token limited to this bucket. Save its access key ID and secret access key in a GitHub Actions environment restricted to the protected producer branch. The S3 endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`, including the bucket in Lake's endpoint paths. The public hostname serves anonymous GETs. [R2 authentication](https://developers.cloudflare.com/r2/api/tokens/)
4. Commit the endpoint configuration below as `lake-cache.toml`. Set `LAKE_CONFIG` to its absolute path for native Lake. In a trusted upload step only, inject the S3 pair as `LAKE_CACHE_KEY` (`<access-key-id>:<secret-access-key>`) for native Lake or the dedicated package variable for `qatlib-env`. Keep PR jobs and public Pages jobs free of R2 write credentials. GitHub's Pages deployment permission is independent of bucket access.

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

## Choose three keys with different reuse rules

Agree on a stable `<repo>` namespace such as `quantum-atlas-ai/qatlib` on both sides. Use the same toolchain and compatible runner platform. In the paths below, `<tc>` replaces `/` with `--` and `:` with `---` in the toolchain name.

| Layer | Example object key | Contract |
| --- | --- | --- |
| Lean build artifacts | `revisions/<repo>/tc/<tc>/inputs/<INPUT_TREE>.jsonl` alongside `revisions/<repo>/tc/<tc>/<sha>.jsonl` | Hash the ordered Git object IDs of the committed build inputs, with an LF after each ID. A Qatlib profile can use `Qatlib`, `Qatlib.lean`, `lakefile.toml`, `lean-toolchain`, and `lake-manifest.json` in that order. Include any other build-affecting paths in your producer and consumer. Store each `.ltar` as a Lake artifact at `artifacts/<repo>/<hash>.art`. Publish artifacts first, then the full JSONL under both names. Resolve the revision first, then the input tree. An ancestor manifest can seed an incremental build. |
| Incremental document analysis | `docs/<repo>/tc/<tc>/analysis/<compatible>/latest.tar.gz` | Derive `compatible` from a format version, the toolchain, the dependency locks, and doc-build configuration. Restore a consistent SQLite backup with its matching generated state, then let the project's build system invalidate and recompute affected modules. Use one writer or serialize publishers for the mutable `latest` alias. |
| Exact raw HTML | `docs/<repo>/tc/<tc>/html/<fingerprint>.tar.gz` | Hash the format version, generation settings, and canonical Git index entries (mode, object ID, and path) for every HTML-generation input. Exclude the portal and CHANGELOG when they are assembled separately. Probe and verify the archive before installing Lean. A Pages job with a verified hit can assemble and deploy in roughly two minutes on a warm path. |

Keep the document-cache formats in the consuming project. Publish archives before their metadata, record the SHA-256 digest and format, and verify both on download before extracting into a fresh directory. Check archive member paths and types, and preserve prior immutable snapshots for rollback. Treat a clean `404` as a cache miss. Stop or retry explicitly on transport, authorization, and integrity errors.

## Native Lake consumer

This workflow assumes a Lean TOML project with a committed lockfile, `enableArtifactCache = true`, the configuration above, and a `lake test` driver. Adapt the targets and ordered paths together with the producer. Projects with large dependencies may also need their own dependency-cache step. This job runs with Git, curl, and Lake from the repository's pinned Lean toolchain. [Lake cache commands](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/)

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

## Managed CI consumer

Set the repository profile in `qatlib-env.toml` so both histories resolve the same namespace. Set `tests` if the project has an additional build target for its test driver. Commit the same Lake read service on both sides. The example pins the CLI to v0.3.0, which must be available before using this workflow. The dedicated CI runner is authorized to compile on a cache miss, subject to its resource limits.

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

For a separate trusted producer, run the managed build on protected `main` with its package-scoped write key injected only in that job. Set `defaultUploadService` to the S3 service. A native producer can use `lake cache stage` / `put-staged` and a signed S3 PUT for the full manifest aliases after verification. Keep the producer's build and audit gates ahead of publication.

## Verify the handoff

1. **Access.** Upload a harmless probe with the bucket-scoped identity. Run `curl -q -fsS https://cache.example.com/probe.txt` without credentials and compare the bytes. Try a credential-free PUT of a disposable object to the public hostname and confirm rejection.
2. **Identity.** Run `qatlib-env cache inspect --json` in two clean checkouts with different SHAs and the same inputs. Compare `paths` and `input_tree`. For an explicit order repeat `--path` once per path. Also compare the document compatibility key and HTML fingerprint from the project's own scripts.
3. **Lean.** Run `qatlib-env cache inspect --remote --json` on the consumer. The new revision can return `404` while the tree alias returns `200` with a positive `entry_count`. Then restore artifacts, verify `lake --no-build build Qatlib`, and run the kernel audit. After the trusted producer records the consumer SHA alias, verify that alias too.
4. **Pages.** Confirm the exact archive restores and verifies before any Lean setup. Test a missing key, interrupted download, and bad digest separately. The missing key may take a credential-free local build path. Failed transport or verification should be visible as an error.

`cache inspect` hashes committed `HEAD` objects. Its default path list comes from the checked-out profile, so run it from the matching clean checkout. `--remote` uses curl for anonymous GETs of both manifests and checks the Lake mapping structure. An HTTP `404` is a successful observation. Transport errors, unexpected HTTP statuses, and invalid manifests produce a nonzero exit status. Artifact validation requires a restore and build probe. See [stage timing and cache fallback](actions-timing.md) for the evidence to collect.
