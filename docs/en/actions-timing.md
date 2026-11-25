# GitHub Actions stage timing and cache fallback

Start with the PR's full head SHA. Open the corresponding workflow run, note its attempt number, and record the job and step start and completion times. A rerun can have different jobs, downloads, and queue time. Use the Actions UI or retrieve the evidence from the CLI:

```sh
qatlib-forge pr checks 123 --repo "$REPO" --platform github --expected-head "$HEAD_SHA"
gh api "repos/$REPO/actions/runs/$RUN_ID/attempts/$ATTEMPT/jobs" --paginate
```

`qatlib-forge` reports `run_attempt`, job URLs, and each step's status, timestamps, and `duration_seconds` when available. A queued job has no execution duration. A running step has no completion timestamp yet. Compare runs with the same workflow, runner class, toolchain, and cache outcome. Use the job's start time and queued state to separate waiting for a runner from a stalled command. [GitHub Actions jobs API](https://docs.github.com/en/rest/actions/workflow-jobs)

## Follow the last phase that started

| Phase | Evidence to look for |
| --- | --- |
| Manifest lookup | `r2-manifest` reports the candidate key and HTTP status. A revision miss followed by an input-tree hit is normal across Git histories. A `200` establishes that a manifest was returned. Check for a usable map and successful artifact recovery. |
| R2 artifact download | `r2-artifacts` measures fetching missing objects for one package. A long transfer can be normal on a cold runner. Compare the missing object count with the last successful run and check for timeouts or retries. |
| Local `.ltar` materialization | Work continues after transfer while Lake validates mappings, restores outputs, and updates local traces. `ltar-restore` measures the fallback's local archive link/copy pass. Lake can also materialize outputs during the no-build probe. Check disk and runner activity when the download ended and this work is still running. |
| No-build probe | `CACHE_HIT kind=exact` identifies a candidate. A completed `no-build` phase with `result=verified` and exit code 0 establishes that the requested build targets needed no rebuild. Look for an emitted result in the log body. Actions prints the command text before executing it. |
| Compilation fallback | `no-build` with `result=rejected` or `result=skipped` can be followed by `fallback-build`. Its reason is `no-build-rejected`, `ancestor`, or `none`. Inspect the Lake module log to see what was rebuilt. A successful job or a step named “restore” can include compilation. `output-manifest` measures manifest completion separately. |
| Lint and audit | `lake-lint` and `lake-test` are separate phases. When the project's test driver audits axioms, use its start marker and completion time. A job step can include both compilation of the driver and its kernel audit. A verified library build still needs the audit. |
| Pages snapshot | Measure the HTTP GET, digest verification, extraction, site assembly, and deploy separately. A verified hit can skip Lean setup. On `404`, follow the configured credential-free generation path and check analysis, rendering, and graph generation. |

`qatlib-env ci build` emits `QATLIB_PHASE` JSON on stderr with `state` (`start`, `done`, or `error`) and `elapsed_s` on completion. Its preparation, no-build, fallback, output-manifest, lint, and test phases are also summarized in `GITHUB_STEP_SUMMARY` when set. After an error or cancellation, inspect the command log for the last phase that started.

In one Lean build, dependency and project cache downloads took about two minutes, and a combined restore/build step took about twelve. Its audit step lasted nearly three minutes, with about one minute after the audit driver announced it had started. One verified HTML snapshot run finished Pages deployment in about two and a half minutes. Use comparable runs before assigning a time budget.

## Distinguish a missing snapshot from a failed download

Record both curl's process exit status and its HTTP status. Successful transport with `404` establishes an absent object at that URL. An HTTP/2 stream failure (curl exit 92) leaves absence unconfirmed, even if some headers or bytes arrived. Retry the same anonymous GET with a bounded timeout and `--http1.1` if the runner's HTTP/2 path is unreliable. Require a successful transfer and the expected digest before extracting. Surface `403`, persistent transport errors, and invalid archives in the job log. [curl exit codes](https://curl.se/libcurl/c/libcurl-errors.html)

Use [the R2 setup guide](r2-cache.md) to verify the bucket, namespace, toolchain key, and the producer's artifact-before-manifest publish order.
