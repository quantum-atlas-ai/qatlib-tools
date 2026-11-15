from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import Request, urlopen


def download_artifact(url: str, destination: str | Path, *, expected_sha256: str | None = None,
                      max_bytes: int = 2_000_000_000) -> Path:
    destination = Path(destination); digest = hashlib.sha256(); total = 0
    with urlopen(Request(url, headers={"Accept": "application/zip", "User-Agent": "qatlib-tools"}), timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes: raise ValueError("artifact exceeds size limit")
            digest.update(chunk); output.write(chunk)
    if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
        raise ValueError("artifact digest mismatch")
    return destination

