"""Verify release version, lockfile and changelog, and extract release notes."""
import argparse
from pathlib import Path
import re
import tomllib

from packaging.version import Version


def metadata(root: Path, tag: str | None = None) -> tuple[Version, str]:
    raw = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    version = Version(raw)
    expected = f"v{version}"
    if str(version) != raw or version.epoch or version.local or len(expected) > 128:
        raise ValueError("project version must be canonical PEP 440 without an epoch or local label")
    if tag is not None and tag != expected:
        raise ValueError(f"tag {tag!r} does not match {expected}")

    packages = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]
    locked = [package.get("version") for package in packages
              if package["name"] == "qatlib-tools" and package.get("source") == {"editable": "."}]
    if locked != [raw]:
        raise ValueError("uv.lock does not match the project version")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    title = r"v\S+ \(\d{4}-\d{2}-\d{2}\)"
    sections = list(re.finditer(rf"^(?:## ({title})|({title})\n-{{3,}})$", changelog, re.MULTILINE))
    matching = [index for index, heading in enumerate(sections)
                if (heading[1] or heading[2]).split()[0] == expected]
    if len(matching) != 1:
        raise ValueError(f"expected exactly one dated changelog section for {expected}")
    index = matching[0]
    end = sections[index + 1].start() if index + 1 < len(sections) else len(changelog)
    notes = changelog[sections[index].end():end].strip()
    if not notes:
        raise ValueError(f"empty changelog section for {expected}")
    return version, notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes", required=True, type=Path)
    args = parser.parse_args()
    try:
        version, notes = metadata(Path.cwd(), args.tag)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    args.notes.write_text(notes + "\n", encoding="utf-8")
    print(f"prerelease={str(version.is_prerelease).lower()}")


if __name__ == "__main__":
    main()
