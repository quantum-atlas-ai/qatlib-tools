# Contributing Guide

Before changing a shared tool, check which version projects pin and how they call it. Give each command a clear purpose so it can be combined with others. Each project maintains its own rules and acceptance criteria.

For local verification, use Python 3.12 to run the same checks as CI. If `uv` is not installed in the current environment, you can still complete the build, run the remaining tests, and validate the offline manual in an isolated environment; see [Tooling Repository Self-Test and Offline Manual Validation](README.en.md#self-test) for the exact commands.

For details on project positioning, review rules, or this repository's design principles and maintenance boundaries, refer to the relevant documentation and human review contract in [DESIGN.en.md](DESIGN.en.md).

Run `python3 scripts/check_human_contracts.py` to verify the digest of the protected block. In a draft, add `--draft` to print the candidate digest. The public hygiene test checks for common credentials and personal absolute paths. Set `QATLIB_HYGIENE_DENYLIST` to add case-insensitive terms, one per line.
