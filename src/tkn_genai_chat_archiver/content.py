"""Streaming hashes of stored bytes and of their decoded content (zstd, gzip)."""

from __future__ import annotations

import hashlib
import zlib
from typing import IO, Any, cast

import zstandard

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
SQLITE_MAGIC = b"SQLite format 3\x00"


class _HashSink:
    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.size += len(data)
        return len(data)


class StreamDigest:
    """Hash bytes as they stream; also hash the decoded content and a byte prefix."""

    def __init__(self, *, prefix_length: int | None = None) -> None:
        self.digest = hashlib.sha256()
        self.size = 0
        self._head = b""
        self._encoding: str | None = None
        self._content = _HashSink()
        self._zstd_writer: Any = None
        self._gzip: Any = None
        self._error: str | None = None
        self._prefix_length = prefix_length
        self._prefix_digest: str | None = None

    def _start_decoder(self) -> None:
        if self._head.startswith(ZSTD_MAGIC):
            self._encoding = "zstd"
            self._zstd_writer = zstandard.ZstdDecompressor().stream_writer(
                cast(IO[bytes], self._content), write_return_read=True
            )
        elif self._head.startswith(GZIP_MAGIC):
            self._encoding = "gzip"
            self._gzip = zlib.decompressobj(wbits=31)
        else:
            self._encoding = "identity"

    def _decode(self, chunk: bytes) -> None:
        if self._error is not None or not chunk:
            return
        try:
            if self._encoding == "zstd":
                self._zstd_writer.write(chunk)
            elif self._encoding == "gzip":
                data = chunk
                while data:
                    self._content.write(self._gzip.decompress(data))
                    if self._gzip.eof:
                        # Concatenated gzip members.
                        data = self._gzip.unused_data
                        self._gzip = zlib.decompressobj(wbits=31)
                    else:
                        data = b""
        except (zstandard.ZstdError, zlib.error) as exc:
            self._error = f"{type(exc).__name__}: {exc}"

    def update(self, chunk: bytes) -> None:
        if self._prefix_length is not None and self._prefix_digest is None:
            remaining = self._prefix_length - self.size
            if remaining <= len(chunk):
                head = self.digest.copy()
                head.update(chunk[:remaining])
                self._prefix_digest = head.hexdigest()
        self.digest.update(chunk)
        self.size += len(chunk)
        if self._encoding is None:
            self._head += chunk
            if len(self._head) < 4 and self.size < 4:
                return
            self._start_decoder()
            buffered, self._head = self._head, b""
            self._decode(buffered)
            return
        self._decode(chunk)

    def finish(self) -> dict[str, Any]:
        if self._encoding is None:
            self._start_decoder()
            buffered, self._head = self._head, b""
            self._decode(buffered)
        if self._encoding == "identity":
            content: dict[str, Any] = {
                "encoding": "identity",
                "byteCount": self.size,
                "sha256": self.digest.hexdigest(),
            }
        else:
            if self._error is None and self._encoding == "zstd":
                try:
                    self._zstd_writer.flush()
                except zstandard.ZstdError as exc:
                    self._error = f"{type(exc).__name__}: {exc}"
            if self._error is None and self._encoding == "gzip" and not self._gzip.eof and self.size:
                self._error = "incomplete gzip stream"
            content = {"encoding": self._encoding}
            if self._error is None:
                content.update(byteCount=self._content.size, sha256=self._content.digest.hexdigest())
            else:
                content["error"] = self._error
        return content

    @property
    def sha256(self) -> str:
        return self.digest.hexdigest()

    @property
    def prefix_sha256(self) -> str | None:
        if self._prefix_length == 0:
            return hashlib.sha256().hexdigest()
        return self._prefix_digest
