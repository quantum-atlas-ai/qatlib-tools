---
layout: home
description: "Engineering handbook and reusable Lean tooling for Qatlib"

hero:
  name: "qatlib-tools"
  text: "Engineering handbook and reusable Lean tooling for Qatlib"
  tagline: "Check that the commands run, edit one module in a second worktree, then read the checks on that commit. Cache, runners, and review rules stay in the later guides."
  actions:
    - theme: brand
      text: Check the tools in five minutes →
      link: /en/docs/quickstart
    - theme: alt
      text: Work in a Lean repository
      link: /en/docs/worktrees
    - theme: alt
      text: Maintainers and runners
      link: /en/docs/github-ci

features:
  - title: Confirm the tools in five minutes
    details: From a checkout of this tools repository, run uv tool install ., then qatlib-env docs --list and qatlib-env config explain --json. This step does not download a Lean toolchain.
    link: /en/docs/quickstart
    linkText: Start with the five-minute check
  - title: One edit in a second worktree
    details: Create a worktree in a Lean repository that already pins the tools. Plan, then prepare, build one module, and check the file you edited with --no-build. Cleanup is a preview.
    link: /en/docs/worktrees
    linkText: Walk through one edit
  - title: A first draft and the checks on this commit
    details: Push the branch, open a draft, record the commands, results, and checks you did not run, then read Checks for that head SHA. CI in this repository also runs for documentation edits.
    link: /en/docs/pull-requests
    linkText: Open the first draft
  - title: Workflows, Pages, and credentials
    details: CI and the documentation site are two workflows. A green CI check on a pull request does not mean Pages deployed. A registration token, GITHUB_TOKEN, and a mirror key each have their own entry.
    link: /en/docs/github-ci
    linkText: Read the two workflows
  - title: Self-hosted runners
    details: The service account follows the lane. Instances of one lane share that account and keep separate work directories, registration names, and resource drop-ins. Page cache and leftover processes are indexed by symptom.
    link: /en/docs/runner
    linkText: Accept a deployment in order
  - title: Look up the rest when you need it
    details: Cache switches, retrieval and bibliographies, cross-repository promotion, and the design contract each start from a task and link to the mechanism.
    link: /en/docs/build
    linkText: Open the build and cache reference
---

<div class="vp-doc carbon-home-strip">

## Confirm the commands first

Run this from the root of a checkout of this tools repository. Pinned commits, wheels, Windows, and a downstream repository's locked version are on the [install page](/en/docs/install).

```bash
uv tool install .
qatlib-env docs --list
qatlib-env config explain --json
```

`qatlib-env docs` prints the Chinese handbook. These English pages are the site's English locale.

</div>
