"""Read-only, chunk-cached FUSE view of a remote HTTP range-served file."""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any

from fuse import FUSE, FuseOSError, Operations


CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class RangeFileSystem(Operations):
    """Expose `/payload.bin` and retrieve its contents in fixed-size chunks."""

    def __init__(
        self,
        source_url: str,
        *,
        chunk_size: int = 1024 * 1024,
        cache_size: int = 16 * 1024 * 1024,
        request_timeout: float = 10,
    ) -> None:
        if chunk_size < 1 or cache_size < chunk_size:
            raise ValueError("cache_size must be at least one chunk and chunk_size must be positive")
        self.source_url = source_url
        self.chunk_size = chunk_size
        self.cache_size = cache_size
        self.request_timeout = request_timeout
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.RLock()
        self._file_size = self._get_file_size()

    def _get_file_size(self) -> int:
        request = urllib.request.Request(self.source_url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                if response.headers.get("Accept-Ranges", "").lower() != "bytes":
                    raise OSError("origin does not advertise byte-range support")
                return int(response.headers["Content-Length"])
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            raise OSError(f"cannot read payload metadata from range origin: {exc}") from exc

    def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
        now = time.time()
        if path == "/":
            return {
                "st_mode": stat.S_IFDIR | 0o555,
                "st_nlink": 2,
                "st_size": 0,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
            }
        if path == "/payload.bin":
            return {
                "st_mode": stat.S_IFREG | 0o444,
                "st_nlink": 1,
                "st_size": self._file_size,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
            }
        raise FuseOSError(errno.ENOENT)

    def readdir(self, path: str, fh: int) -> list[str]:
        if path != "/":
            raise FuseOSError(errno.ENOENT)
        return [".", "..", "payload.bin"]

    def open(self, path: str, fi: Any) -> int:
        if path != "/payload.bin":
            raise FuseOSError(errno.ENOENT)
        if (fi.flags & os.O_ACCMODE) != os.O_RDONLY:
            raise FuseOSError(errno.EROFS)
        # Bypass the kernel page cache so repeated reads exercise this PoC's
        # own bounded LRU cache. Production filesystems usually tune this.
        fi.direct_io = True
        return 0

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        if path != "/payload.bin":
            raise FuseOSError(errno.ENOENT)
        if offset < 0 or size < 0:
            raise FuseOSError(errno.EINVAL)
        if size == 0 or offset >= self._file_size:
            return b""

        remaining = min(size, self._file_size - offset)
        result = bytearray()
        while remaining:
            chunk_index = offset // self.chunk_size
            chunk_offset = offset % self.chunk_size
            try:
                chunk = self._get_chunk(chunk_index)
            except OSError as exc:
                raise FuseOSError(errno.EIO) from exc
            take = min(remaining, len(chunk) - chunk_offset)
            if take <= 0:
                raise FuseOSError(errno.EIO)
            result.extend(chunk[chunk_offset : chunk_offset + take])
            offset += take
            remaining -= take
        return bytes(result)

    def _get_chunk(self, chunk_index: int) -> bytes:
        with self._lock:
            cached = self._cache.get(chunk_index)
            if cached is not None:
                self._cache.move_to_end(chunk_index)
                return cached

            start = chunk_index * self.chunk_size
            if start >= self._file_size:
                return b""
            end = min(start + self.chunk_size, self._file_size) - 1
            expected_size = end - start + 1
            request = urllib.request.Request(
                self.source_url,
                headers={"Range": f"bytes={start}-{end}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                    if response.status != 206:
                        raise OSError(f"origin returned HTTP {response.status}, expected 206")
                    match = CONTENT_RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
                    if not match or tuple(map(int, match.groups())) != (start, end, self._file_size):
                        raise OSError("origin returned a missing or incorrect Content-Range header")
                    body = response.read(expected_size + 1)
            except urllib.error.HTTPError as exc:
                raise OSError(f"range request failed with HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise OSError(f"range origin request failed: {exc.reason}") from exc

            if len(body) != expected_size:
                raise OSError(f"origin returned {len(body)} bytes for a {expected_size}-byte range")
            self._cache[chunk_index] = body
            self._cache_bytes += len(body)
            while self._cache_bytes > self.cache_size and self._cache:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted)
            return body


def mount_filesystem(mountpoint: str, source_url: str) -> None:
    fs = RangeFileSystem(
        source_url,
        chunk_size=int(os.environ.get("FUSE_CHUNK_SIZE", str(1024 * 1024))),
        cache_size=int(os.environ.get("FUSE_CACHE_SIZE", str(16 * 1024 * 1024))),
        request_timeout=float(os.environ.get("FUSE_REQUEST_TIMEOUT", "10")),
    )
    print(
        f"mounting {mountpoint}: source_size={fs._file_size}, "
        f"chunk_size={fs.chunk_size}, cache_size={fs.cache_size}",
        flush=True,
    )
    FUSE(
        fs,
        mountpoint,
        foreground=True,
        nothreads=False,
        raw_fi=True,
        ro=True,
        fsname="python-range-fuse",
# By passing attr_timeout=0 and entry_timeout=0, it ensures that the kernel immediately checks back with our Python code for every file lookup
        attr_timeout=0,
        entry_timeout=0,
    )
