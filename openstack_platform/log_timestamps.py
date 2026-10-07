"""Prefix build output as the admin helper receives it, within its file limit."""

from __future__ import annotations

import io
from collections.abc import Buffer
from datetime import UTC, datetime
from typing import BinaryIO

MAX_LINE_BYTES = 8192


class TimestampedBuildLog(io.BufferedIOBase):
    """Streaming byte sink; flush exposes partial lines without buffering them."""

    def __init__(self, destination: BinaryIO, maximum: int) -> None:
        self.destination = destination
        self.maximum = maximum
        self.line_bytes = 0
        self.continued = False
        super().__init__()

    def tell(self) -> int:
        return self.destination.tell()

    def _record(self, data: bytes) -> None:
        room = max(0, self.maximum - self.tell())
        if room:
            self.destination.write(data[:room])

    def write(self, buffer: Buffer) -> int:
        data = bytes(buffer)
        offset = 0
        while offset < len(data):
            newline = data.find(b"\n", offset)
            end = len(data) if newline == -1 else newline
            count = min(end - offset, MAX_LINE_BYTES - self.line_bytes)
            if count or not self.continued:
                if not self.line_bytes:
                    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
                    self._record(timestamp.replace("+00:00", "Z").encode() + b" ")
                self._record(data[offset : offset + count])
                self.line_bytes += count
                offset += count
            if offset == newline:
                if self.line_bytes or not self.continued:
                    self._record(b"\n")
                self.line_bytes = 0
                self.continued = False
                offset += 1
            elif self.line_bytes == MAX_LINE_BYTES:
                self._record(b"\n")
                self.line_bytes = 0
                self.continued = True
        return len(data)

    def flush(self) -> None:
        self.destination.flush()

    def close(self) -> None:
        if not self.closed and self.line_bytes:
            self._record(b"\n")
            self.line_bytes = 0
        super().close()
