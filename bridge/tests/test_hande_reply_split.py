"""A long final answer is split into page-numbered parts (#2856)."""

from __future__ import annotations

import re

from telegram_bot.core.hande.reply_split import answer_block_start, split_reply

SINGLE = 8192
PART = SINGLE - 128
MARKER = re.compile(r"^\((\d+)/(\d+)\)\n\n")


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _split(text: str, single: int = SINGLE, part: int = PART) -> list[str]:
    return split_reply(text, single_bytes=single, part_bytes=part)


def _bodies(parts: list[str]) -> list[str]:
    """Parts without their marker, checking the numbering on the way."""

    bodies = []
    for index, part in enumerate(parts, 1):
        match = MARKER.match(part)
        assert match, part[:40]
        assert (int(match.group(1)), int(match.group(2))) == (index, len(parts))
        bodies.append(part[match.end():])
    return bodies


def _fences_balanced(text: str) -> bool:
    return sum(1 for line in text.split("\n") if line.lstrip().startswith("```")) % 2 == 0


def test_answer_within_the_bound_is_returned_unchanged_and_unmarked() -> None:
    text = "가" * (SINGLE // 3)  # 3 bytes each: just under the bound
    assert _size(text) <= SINGLE
    assert _split(text) == [text]
    assert _split("") == [""]
    exact = "a" * SINGLE
    assert _split(exact) == [exact]


def test_one_byte_over_the_bound_splits_and_every_part_fits() -> None:
    text = "\n\n".join("문단 %d. " % n + "가나다라마바사 " * 40 for n in range(40))
    assert _size(text) > SINGLE
    parts = _split(text)
    assert len(parts) >= 2
    assert all(_size(part) <= PART for part in parts)
    # Nothing is lost or reordered: the paragraphs come back in order.
    joined = "\n\n".join(_bodies(parts))
    assert [line for line in joined.split("\n") if line.strip()] == [
        line for line in text.split("\n") if line.strip()
    ]


def test_cuts_land_on_paragraph_boundaries_not_inside_one() -> None:
    paragraphs = ["문단 %02d " % n + "내용 " * 150 for n in range(30)]
    parts = _split("\n\n".join(paragraphs))
    for body in _bodies(parts):
        assert body.startswith("문단 ")
        assert body.rstrip().endswith("내용")


def test_heading_starts_a_part_when_one_is_near_the_end() -> None:
    sections = ["## 절 %d\n\n" % n + "\n\n".join("줄 " * 120 for _ in range(6)) for n in range(8)]
    parts = _split("\n\n".join(sections))
    bodies = _bodies(parts)
    assert len(bodies) >= 2
    assert all(body.startswith("## 절 ") for body in bodies)


def test_list_items_are_not_cut_in_the_middle() -> None:
    items = ["- 항목 %03d: " % n + "설명 " * 30 for n in range(200)]
    parts = _split("\n".join(items))
    for body in _bodies(parts):
        for line in body.split("\n"):
            assert line.startswith("- 항목 ") and line.rstrip().endswith("설명")


def test_code_fence_cut_is_closed_and_reopened_with_its_language() -> None:
    code = "\n".join("print(%d)  # %s" % (n, "x" * 60) for n in range(400))
    text = "머리말\n\n```python\n" + code + "\n```\n\n맺음말"
    parts = _split(text)
    bodies = _bodies(parts)
    assert len(bodies) >= 3
    assert all(_fences_balanced(body) for body in bodies)
    assert all(_size(part) <= PART for part in parts)
    # A part that continues the block reopens it with the same opener.
    assert bodies[1].startswith("```python\n")
    # Every code line survives exactly once.
    lines = [line for body in bodies for line in body.split("\n") if line.startswith("print(")]
    assert lines == code.split("\n")
    assert bodies[-1].rstrip().endswith("맺음말")


def test_table_cut_repeats_the_header_rows() -> None:
    header = "| 번호 | 이름 | 설명 |\n| --- | --- | --- |"
    rows = "\n".join("| %d | 항목 | %s |" % (n, "값 " * 20) for n in range(300))
    parts = _split("표입니다.\n\n" + header + "\n" + rows + "\n\n끝.")
    bodies = _bodies(parts)
    assert len(bodies) >= 2
    for body in bodies[1:]:
        if body.startswith("|"):
            assert body.startswith(header + "\n| ")
    seen = [line for body in bodies for line in body.split("\n") if re.match(r"\| \d+ \|", line)]
    assert seen == rows.split("\n")


def test_answer_block_stays_whole_in_the_last_part() -> None:
    block = "**[답변 필요]** 하나만 선택\n1. (권장) 첫째 안\n2. 둘째 안\n3. 여기서 마치기"
    text = "\n\n".join("설명 문단 %d " % n + "가 " * 300 for n in range(20)) + "\n\n" + block
    parts = _split(text)
    bodies = _bodies(parts)
    assert len(bodies) >= 2
    assert bodies[-1].endswith(block)
    assert all("[답변 필요]" not in body for body in bodies[:-1])


def test_answer_block_gets_its_own_part_when_the_last_one_is_full() -> None:
    block = "**[답변 필요]** 여러 개 선택 가능\n" + "\n".join(
        "%d. 선택지 %s" % (n, "가" * 60) for n in range(1, 20)
    )
    filler = "\n\n".join("가" * 2000 for _ in range(4))  # 24 KiB of prose
    parts = _split(filler + "\n\n" + block)
    assert _bodies(parts)[-1] == block


def test_answer_block_is_never_split_whatever_falls_before_it() -> None:
    """Sweep the prose length so the block lands on every side of a part boundary."""

    # Loose list: the blank lines between options are paragraph boundaries, which
    # the packer would otherwise prefer over the block's own first line.
    block = "**[답변 필요]** 하나만 선택\n\n" + "\n\n".join(
        "%d. 선택지 %s" % (n, "나" * 25) for n in range(1, 11)
    )
    for lines in range(60, 180):
        prose = "\n".join("줄 %03d %s" % (n, "가" * 30) for n in range(lines))
        parts = _split(prose + "\n\n" + block)
        if len(parts) < 2:
            continue
        bodies = _bodies(parts)
        assert bodies[-1].endswith(block), lines
        assert all("선택지" not in body for body in bodies[:-1]), lines


def test_answer_header_inside_a_code_fence_is_not_a_block() -> None:
    text = "```\n**[답변 필요]** 하나만 선택\n1. 예\n```\n본문"
    assert answer_block_start(text) is None
    assert answer_block_start("본문\n\n[답변 필요]\n\n1. 예\n2. 아니오") == _len_prefix("본문\n\n")
    # A header with no numbered option under it is prose, not a card.
    assert answer_block_start("[답변 필요] 라는 말만 있음\n다음 줄") is None


def _len_prefix(prefix: str) -> int:
    return len(prefix)


def test_one_line_longer_than_a_part_is_cut_at_a_character_boundary() -> None:
    text = "가" * 10000  # 30 KiB, no newline, no space
    parts = _split(text)
    assert all(_size(part) <= PART for part in parts)
    assert "".join(_bodies(parts)) == text


def test_long_line_prefers_a_space_near_the_end() -> None:
    text = " ".join(["word"] * 6000)  # 30 KiB on one line
    parts = _split(text)
    bodies = _bodies(parts)
    assert "".join(bodies) == text
    assert all(not body.endswith("wor") and not body.startswith("ord") for body in bodies)


def test_twenty_kib_korean_answer_with_every_structure() -> None:
    """The issue's acceptance case: headings, a list, a table, a code block, a block."""

    table = "| 항목 | 값 |\n| --- | --- |\n" + "\n".join("| 행 %d | %s |" % (n, "나" * 30) for n in range(40))
    code = "```bash\n" + "\n".join("echo %d %s" % (n, "y" * 50) for n in range(60)) + "\n```"
    block = "**[답변 필요]** 하나만 선택\n1. (권장) 진행\n2. 보류"
    sections = []
    for n in range(6):
        sections.append("## 절 %d\n\n%s" % (n, "\n\n".join("가나다라 " * 60 for _ in range(4))))
        sections.append("\n".join("- 목록 %d-%d %s" % (n, k, "다 " * 20) for k in range(8)))
    text = "\n\n".join([*sections, table, code, "마지막 설명.", block])
    assert _size(text) > 20 * 1024
    parts = _split(text)
    bodies = _bodies(parts)
    assert all(_size(part) <= PART for part in parts)
    assert all(_fences_balanced(body) for body in bodies)
    assert bodies[-1].endswith(block)
    assert "…" not in "".join(parts)  # nothing is cut away
    # No blank-only part and no part that starts with a blank line.
    assert all(body.strip() and not body.startswith("\n") for body in bodies)


def test_small_bounds_never_loop_or_return_oversize_parts() -> None:
    text = ("한글 문장 하나. " * 30 + "\n") * 20
    for single in (200, 300, 1024):
        parts = _split(text, single=single, part=single - 16)
        assert len(parts) >= 2
        assert all(_size(part) <= single - 16 for part in parts)
    # A part bound too small to hold anything useful leaves the answer alone.
    assert _split(text, single=64, part=40) == [text]


def test_config_knobs_default_on_and_are_bounded() -> None:
    import pytest

    from telegram_bot.core.hande.state import HandeConfigError, validate_config

    base = {
        "account": "@bridgenode:hande",
        "owner": "@owner:hande",
        "rooms": ["agent"],
        "socket_path": "/run/ccc-hande/responder.sock",
    }
    knobs = validate_config(base)
    assert knobs["reply_split"] is True
    assert (knobs["bot_reply_max_bytes"], knobs["bot_interim_max"]) == (8192, 40)
    off = validate_config({**base, "reply_split": False, "bot_interim_max": 0})
    assert off["reply_split"] is False and off["bot_interim_max"] == 0
    for bad in (
        {"reply_split": "yes"},
        {"bot_reply_max_bytes": 512},
        {"bot_reply_max_bytes": 1 << 20},
        {"bot_reply_max_bytes": "8192"},
        {"bot_interim_max": -1},
        {"bot_interim_max": 1001},
        {"bot_interim_max": True},
    ):
        with pytest.raises(HandeConfigError):
            validate_config({**base, **bad})
