"""Guard the protected-path inventory and its GitHub ownership."""
from pathlib import Path
import re


def test_human_review_paths():
    root = Path(__file__).resolve().parents[1]
    required = {"README.md", "AGENTS.md", "CONTRIBUTING.md", ".github/CODEOWNERS",
                ".github/workflows/ci.yml", "tests/test_human_review_paths.py"}
    inventory = set(re.findall(r"(?m)^- `([^`]+)`$", (root / "CONTRIBUTING.md").read_text()))
    assert required <= inventory
    rules = {}
    for line in (root / ".github/CODEOWNERS").read_text().splitlines():
        if line and not line.startswith("#"):
            path, *owners = line.split()
            assert path not in rules
            rules[path] = owners
    assert rules.keys() == {"/" + path for path in inventory}
    for path in inventory:
        assert (root / path).is_file()
        assert rules["/" + path] and all(re.fullmatch(r"@[A-Za-z0-9-]+", owner)
                                           for owner in rules["/" + path])
