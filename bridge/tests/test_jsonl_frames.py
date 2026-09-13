"""``read_jsonl_frame`` — frames larger than the stream limit are read whole (#1718)."""

from __future__ import annotations

import asyncio
import json
import unittest

from telegram_bot.core.jsonl_frames import FrameTooLargeError, read_jsonl_frame


def _reader(limit: int) -> asyncio.StreamReader:
    return asyncio.StreamReader(limit=limit)


class ReadJsonlFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_larger_than_stream_limit_is_returned_whole(self) -> None:
        big = json.dumps({"id": 1, "result": {"blob": "x" * (100 * 1024)}}).encode()

        # The plain readline contract this replaces raises here.
        probe = _reader(limit=1024)
        probe.feed_data(big + b"\n")
        with self.assertRaises(ValueError):
            await probe.readline()

        reader = _reader(limit=1024)
        reader.feed_data(big + b"\n")
        reader.feed_data(b'{"id":2}\n')
        reader.feed_eof()

        first = await read_jsonl_frame(reader)
        self.assertEqual(first, big + b"\n")
        self.assertEqual(json.loads(first)["result"]["blob"], "x" * (100 * 1024))
        # The following small frame is intact — no bytes were lost at chunk seams.
        self.assertEqual(await read_jsonl_frame(reader), b'{"id":2}\n')
        self.assertEqual(await read_jsonl_frame(reader), b"")

    async def test_separator_found_past_limit_is_reassembled(self) -> None:
        # Second LimitOverrunError shape: the whole line, separator included,
        # is already buffered when readuntil runs.
        reader = _reader(limit=64)
        reader.feed_data(b"a" * 500 + b"\n" + b"b" * 10 + b"\n")
        reader.feed_eof()
        self.assertEqual(await read_jsonl_frame(reader), b"a" * 500 + b"\n")
        self.assertEqual(await read_jsonl_frame(reader), b"b" * 10 + b"\n")
        self.assertEqual(await read_jsonl_frame(reader), b"")

    async def test_small_frames_behave_like_readline(self) -> None:
        reader = _reader(limit=65536)
        reader.feed_data(b'{"a":1}\n{"b":2}\n')
        reader.feed_eof()
        self.assertEqual(await read_jsonl_frame(reader), b'{"a":1}\n')
        self.assertEqual(await read_jsonl_frame(reader), b'{"b":2}\n')
        self.assertEqual(await read_jsonl_frame(reader), b"")

    async def test_unterminated_tail_is_returned_at_eof(self) -> None:
        reader = _reader(limit=16)
        reader.feed_data(b"z" * 40)  # over the limit and never terminated
        reader.feed_eof()
        self.assertEqual(await read_jsonl_frame(reader), b"z" * 40)
        self.assertEqual(await read_jsonl_frame(reader), b"")

    async def test_hard_cap_fails_closed(self) -> None:
        reader = _reader(limit=256)
        reader.feed_data(b"q" * 5000 + b"\n")
        reader.feed_eof()
        with self.assertRaises(FrameTooLargeError):
            await read_jsonl_frame(reader, max_frame_bytes=4096)

    async def test_reader_without_readuntil_falls_back_to_readline(self) -> None:
        class LineOnly:
            def __init__(self) -> None:
                self.calls = 0

            async def readline(self) -> bytes:
                self.calls += 1
                return b'{"x":1}\n' if self.calls == 1 else b""

        stub = LineOnly()
        self.assertEqual(await read_jsonl_frame(stub), b'{"x":1}\n')
        self.assertEqual(await read_jsonl_frame(stub), b"")
