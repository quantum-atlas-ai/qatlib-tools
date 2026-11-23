"""Prepare an exact staging tree as one commit on top of public main."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from release_metadata import metadata

PUBLIC_URL = "https://github.com/quantum-atlas-ai/qatlib-tools.git"
MAIN = "refs/heads/main"


def git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], env=env, text=True).strip()


def snapshot(source: Path, ref: str, checkout: Path, *, push: bool = False) -> dict:
    local_env = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True).splitlines()
    env = {name: value for name, value in os.environ.items() if name not in local_env}
    source = Path(git(source, env, "rev-parse", "--show-toplevel"))
    tip = git(source, env, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    ref_name = git(source, env, "rev-parse", "--verify", "--symbolic-full-name", "--end-of-options", ref)
    staging_tree = git(source, env, "rev-parse", f"{tip}^{{tree}}")
    checkout.mkdir()
    git(checkout, env, "clone", "--no-checkout", "--no-tags", "--single-branch", "--branch", "main",
        "--", PUBLIC_URL, ".")
    base = git(checkout, env, "rev-parse", "refs/remotes/origin/main")
    git(checkout, env, "fetch", "--no-tags", "--", str(source), tip)
    git(checkout, env, "switch", "--detach", base)
    git(checkout, env, "read-tree", "--reset", "-u", staging_tree)

    version, _ = metadata(checkout)
    tag = f"v{version}"
    tag_ref = f"refs/tags/{tag}"
    if ref_name.startswith("refs/tags/") and ref_name != tag_ref:
        raise ValueError(f"selected staging tag does not match {tag}")
    selected_tag = git(source, env, "tag", "--list", tag)
    if selected_tag:
        if git(source, env, "cat-file", "-t", tag_ref) != "tag":
            raise ValueError(f"staging tag {tag} must be annotated")
        if git(source, env, "rev-parse", f"{tag_ref}^{{commit}}") != tip:
            raise ValueError(f"staging tag {tag} does not select the requested commit")
    elif push:
        raise ValueError(f"staging requires the annotated tag {tag} for publication")
    if git(checkout, env, "ls-remote", "--refs", "origin", tag_ref):
        raise ValueError(f"public tag {tag} already exists")

    def check(*args: str, **extra_env: str) -> None:
        subprocess.run(args, cwd=checkout, env={**env, **extra_env}, check=True)

    git(checkout, env, "diff", "--cached", "--check")
    check(sys.executable, "-B", "scripts/check_human_contracts.py")
    check(sys.executable, "-B", "-m", "unittest", "tests.test_public_hygiene",
          QATLIB_HYGIENE_STAGED="1")
    check("uv", "lock", "--check")
    cz_version = subprocess.check_output([sys.executable, "-m", "commitizen", "version", "--project"],
                                         cwd=checkout, env=env, text=True).strip()
    if cz_version != str(version):
        raise ValueError("Commitizen version does not match the snapshot")
    git(checkout, env, "commit", "--allow-empty", "-m", f"chore(release): {tag}")
    public_commit = git(checkout, env, "rev-parse", "HEAD")
    public_tree = git(checkout, env, "rev-parse", "HEAD^{tree}")
    if public_tree != staging_tree or git(checkout, env, "show", "-s", "--format=%P", "HEAD") != base:
        raise ValueError("snapshot tree or public parent differs from the verified input")
    if git(checkout, env, "show", "-s", "--format=%(trailers:keyonly)", "HEAD"):
        raise ValueError("the snapshot commit unexpectedly contains trailers")
    if push:
        git(source, env, "merge-base", "--is-ancestor", tip, "refs/remotes/origin/main")
        if git(checkout, env, "remote", "get-url", "--push", "origin") != PUBLIC_URL:
            raise ValueError("the configured push URL differs from the canonical public URL")
        git(checkout, env, "tag", "-a", "-m", tag, tag, public_commit)
        git(checkout, env, "-c", "push.followTags=false", "push", "--atomic",
            f"--force-with-lease={MAIN}:{base}", "origin", f"{public_commit}:{MAIN}", f"{tag_ref}:{tag_ref}")
    return {"staging_commit": tip, "staging_tree": staging_tree, "public_parent": base,
            "public_commit": public_commit, "public_tree": public_tree, "tag": tag, "pushed": push}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="verified staging commit or tag")
    parser.add_argument("--work-dir", required=True, type=Path, help="new directory for the local public checkout")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="prepare locally (default)")
    mode.add_argument("--push", action="store_true", help="publish the verified snapshot and its version tag")
    args = parser.parse_args()
    checkout = args.work_dir.resolve()
    try:
        result = snapshot(Path.cwd(), args.ref, checkout, push=args.push)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
