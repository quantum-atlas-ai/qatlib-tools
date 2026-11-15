from __future__ import annotations

from dataclasses import dataclass, asdict
import io
import re
from urllib.request import Request, urlopen
import zipfile


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    message: str
    severity: str = "error"
    offset: int | None = None


_PATTERNS = (re.compile(r"(?P<path>[^\s:]+\.lean):(?P<line>\d+):(?P<col>\d+):\s*(?P<kind>error|warning):\s*(?P<msg>.*)"),
             re.compile(r"(?P<kind>error|warning):\s*(?P<path>[^\s:]+\.lean):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.*)"))


def extract_diagnostics(text: str, *, base_offset: int = 0) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for match in (m for line in text.splitlines() for pattern in _PATTERNS if (m := pattern.search(line))):
        result.append(Diagnostic(match["path"], int(match["line"]), int(match["col"]), match["msg"].strip(), match["kind"], base_offset))
    return result


class RangeLogReader:
    def __init__(self, url: str, *, opener=urlopen, max_bytes: int = 50_000_000):
        self.url, self.opener, self.max_bytes = url, opener, max_bytes

    def read(self, start: int = 0, end: int | None = None) -> tuple[bytes, dict[str, str]]:
        if start < 0 or (end is not None and end < start): raise ValueError("invalid byte range")
        value = f"bytes={start}-{'' if end is None else end}"
        request = Request(self.url, headers={"Range": value, "Accept": "text/plain"})
        with self.opener(request, timeout=30) as response:
            data = response.read(self.max_bytes + 1)
            headers = {key.lower(): val for key, val in response.headers.items()}
            status = getattr(response, "status", response.getcode())
            if len(data) > self.max_bytes: raise ValueError("log response exceeds limit")
            if start and status == 200: raise ValueError("server ignored explicit range")
            if status not in {200, 206}: raise ValueError(f"unexpected log status {status}")
            if status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", headers.get("content-range", ""))
                if not match or int(match[1]) != start or int(match[2]) < int(match[1]):
                    raise ValueError("invalid Content-Range response")
                if end is not None and int(match[2]) > end:
                    raise ValueError("Content-Range exceeds requested end")
            return data, {**headers, ":status": str(status)}

    def diagnostics(self, start: int = 0, end: int | None = None) -> list[Diagnostic]:
        data, _ = self.read(start, end)
        return extract_diagnostics(data.decode("utf-8", "replace"), base_offset=start)

    def tail(self, size: int = 4096, *, total_size: int | None = None) -> tuple[bytes, dict[str, str]]:
        if size <= 0: raise ValueError("tail size must be positive")
        if total_size is None:
            request = Request(self.url, headers={"Range": f"bytes=-{size}", "Accept": "text/plain"})
            with self.opener(request, timeout=30) as response:
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes: raise ValueError("log response exceeds limit")
                return data, {**{key.lower(): val for key, val in response.headers.items()}, ":status": str(getattr(response, "status", response.getcode()))}
        start = max(0, total_size - size)
        return self.read(start, total_size - 1)


def read_run_zip(data: bytes, *, max_files: int = 10000, max_bytes: int = 200_000_000) -> dict[str, bytes]:
    result: dict[str, bytes] = {}; total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            normalized = info.filename.replace("\\", "/")
            path_parts = normalized.rstrip("/").split("/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or any(part in {"", ".", ".."} for part in path_parts):
                raise ValueError("run log ZIP path traversal")
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("run log ZIP symlink is forbidden")
            if info.is_dir() or len(result) >= max_files or total + info.file_size > max_bytes: continue
            result[info.filename] = archive.read(info); total += info.file_size
    return result

