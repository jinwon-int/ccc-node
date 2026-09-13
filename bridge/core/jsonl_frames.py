"""Read newline-delimited JSON frames without a per-line stream ceiling (#1718).

``asyncio.StreamReader.readline`` raises ``ValueError``/``LimitOverrunError``
("Separator is not found, and chunk exceed the limit") as soon as one line
outgrows the reader's ``limit``. Both JSONL transports in this package (Codex
app-server, Piri RPC) used ``readline`` behind a 16 MiB limit (#403), so a
single oversized frame — measured on seoseo 2026-09-13: a ``thread/resume``
response of 16.48 MiB for a 109-turn thread — tore the whole connection down
and left the client poisoned for every later request.

``read_jsonl_frame`` keeps the stream limit as a *chunk* size: when
``readuntil`` overruns, the already-buffered bytes are consumed and the search
for the separator continues, so the frame is returned whole. A separate,
much larger hard cap (``max_frame_bytes``) still fails closed on a runaway
peer instead of growing memory without bound.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

DEFAULT_MAX_FRAME_BYTES = 512 * 1024 * 1024  # 512 MiB — fail-closed ceiling per frame


class FrameTooLargeError(ValueError):
    """A single JSONL frame exceeded ``max_frame_bytes``."""


class _LineReader(Protocol):
    async def readline(self) -> bytes: ...


async def read_jsonl_frame(
    reader: _LineReader,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> bytes:
    """Return one ``\\n``-terminated frame (or ``b""`` at EOF).

    Semantics match ``readline`` — the trailing newline is included and a
    final unterminated line is returned as-is at EOF — except that a frame
    longer than the reader's stream limit is assembled from chunks instead
    of raising. Readers without ``readuntil`` (test doubles) fall back to
    ``readline`` unchanged.
    """

    readuntil = getattr(reader, "readuntil", None)
    readexactly = getattr(reader, "readexactly", None)
    if readuntil is None or readexactly is None:
        return await reader.readline()

    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = await readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            # Two shapes, both recoverable: the separator is beyond the limit
            # (``consumed`` = buffered bytes so far) or it was found past the
            # limit (``consumed`` = bytes before it). Consume exactly that many
            # and search again; the separator, when present, is still queued.
            if exc.consumed <= 0:
                raise
            chunk = await readexactly(exc.consumed)
            total += len(chunk)
            if total > max_frame_bytes:
                raise FrameTooLargeError(
                    f"JSONL frame exceeds {max_frame_bytes} bytes"
                ) from exc
            chunks.append(chunk)
            continue
        except asyncio.IncompleteReadError as exc:
            # EOF: hand back whatever arrived (possibly nothing), like readline.
            chunks.append(exc.partial)
            return b"".join(chunks)
        total += len(chunk)
        if total > max_frame_bytes:
            raise FrameTooLargeError(f"JSONL frame exceeds {max_frame_bytes} bytes")
        chunks.append(chunk)
        return b"".join(chunks)
