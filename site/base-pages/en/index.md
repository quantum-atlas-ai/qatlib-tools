---
layout: home

hero:
  name: "qatlib-tools"
  text: "Engineering Handbook & Tooling for Lean 4 Repositories"
  tagline: "Field-tested engineering mechanisms and lightweight CLI utilities for shared artifact caching, Runner process isolation, static lexical retrieval, GitHub / Gitea CI verification, and lossless cross-repository promotion."
  actions:
    - theme: brand
      text: Read Engineering Guides →
      link: /en/docs/build
    - theme: alt
      text: Quickstart & CLI
      link: /en/guide
    - theme: alt
      text: Design Principles
      link: /en/design

features:
  - title: Build Environment & Artifact Caching
    details: Understand the 9-state behavior matrix of enableArtifactCache and restoreAllArtifacts in lakefile.toml, Lean 4.35 module visibility, three cache layers, and Windows cross-volume nuances.
    link: /en/docs/build
    linkText: Build & Cache Guide
  - title: Runner Accounting & Orphan Reaping
    details: Diagnose Linux cgroup v2 page-cache accounting stalls and eliminate orphaned Lean processes on job cancellation via parent-death guardians, subreapers, and transient systemd scopes.
    link: /en/docs/runner
    linkText: Runner & Isolation Guide
  - title: Docstrings, Bibliography & Retrieval
    details: Compare full-site doc-gen4 symbol routing with local LSP hover rendering, validate incremental BibTeX bibliographies, and query declarations plus section variables without invoking Lean.
    link: /en/docs/lean-docs
    linkText: Docs & Retrieval Guide
  - title: GitHub & Gitea PR Verification
    details: >-
      Check GitHub's isDraft state and Gitea's draft title prefixes, reconcile job conclusions by commit SHA, and verify the remote commit before merging or deleting a branch.
    link: /en/docs/forge
    linkText: Forge & CI Guide
  - title: Cross-Repo Promotion & Lock Cutover
    details: Extract whole-file dependency closures, verify public interfaces with ordinary module consumer probes, rehearse lock upgrades in Draft PRs, and retire duplicates cleanly after release.
    link: /en/docs/promotion
    linkText: Promotion Guide
  - title: Shared Tooling & Offline Manual
    details: Everyday single-step tasks stay on native git, gh, and tea / gt. The wheel bundles the five engineering guides, and qatlib-env docs serves the Chinese originals offline.
    link: /en/design
    linkText: Design Principles
---

<div class="vp-doc carbon-home-strip">

## Three CLI Entry Points & Offline Manual

A single installation provides `qatlib-env`, `qatlib-retrieve`, and `qatlib-forge`. The five engineering guides are available in Chinese from the terminal.

```bash
# Install standalone CLI tools with uv (pipx and stdlib venv + pip are also supported)
uv tool install 'git+https://github.com/quantum-atlas-ai/qatlib-tools.git'

# Browse and read the exact same engineering guides offline
qatlib-env docs --list
qatlib-env docs build
```

</div>
