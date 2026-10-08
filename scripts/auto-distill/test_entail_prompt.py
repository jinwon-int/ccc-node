#!/usr/bin/env python3
"""Entail rubric definition of "핵심" (#1846).

`ENTAIL_PROMPT` asked whether a quote supports "주장의 핵심 내용" without saying
what the core is. haiku read it as "every component must be quoted" and Jev as
"the central claim is quoted" — both valid readings of the same prompt, so the
same session could land in a different AUTO.md depending on which lane judged
it. The 2026-09-19 stratified 120-item measurement rejected a 3-branch
core/full gate (it pushed 28% of the stream to human review) and converged on
spelling the rubric out in one paragraph instead. These tests pin that
paragraph and pin that nothing else about the verdict contract moved: same
two placeholders, same strict-JSON verdict format, same parsing in `entail()`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "auto-distill.py"

# Verbatim from the #1846 converged comment ("최종 수치 — 120건 완료", 결론).
CORE_DEFINITION = (
    "- 주장의 핵심이 인용문에 있으면 yes다.\n"
    "- 날짜·파일명·줄번호·시각처럼 주장에 딸린 부수 정보가 인용문에 없어도 yes다.\n"
    "- 주장이 단정한 결과·상태·완료가 인용문에 없으면 no다\n"
    "  (목표를 완료로, 존재를 실행으로 바꿔 말한 경우가 여기 해당한다)."
)
VERDICT_FORMAT = '{"supported":"yes|no","why":"15자 이내 사유"}'


def _load():
    spec = importlib.util.spec_from_file_location("entail_prompt_auto_distill", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AD = _load()


def _fake_model(tmpdir: str, verdict_text: str) -> tuple[list[str], Path]:
    """A stand-in model command: records the prompt, answers with `verdict_text`.

    Runs through the current interpreter rather than a `#!/bin/sh` stub so the
    test also works where /bin/sh does not exist (Termux). The answer is wrapped
    in a Claude Code `--output-format json` envelope, the production lane shape.
    """
    capture = Path(tmpdir) / "prompt.txt"
    script = Path(tmpdir) / "fake_model.py"
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 0.001,
        "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 10}},
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "result": verdict_text,
    }
    script.write_text(
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "open(%r, 'wb').write(data)\n"
        "sys.stdout.write(%r)\n" % (str(capture), json.dumps(envelope)),
        encoding="utf-8",
    )
    return [sys.executable, str(script)], capture


def _item(fact: str = "가상 서비스 A는 실패 요청을 재시도한다.") -> dict:
    return {
        "title": "재시도",
        "fact": fact,
        "evidence": ["abcdef12"],
        "_evidence_text": ["가상 서비스 A는 실패 요청을 재시도한다."],
    }


class EntailPromptDefinitionTest(unittest.TestCase):
    def test_prompt_defines_core_with_converged_paragraph(self) -> None:
        self.assertIn(CORE_DEFINITION, AD.ENTAIL_PROMPT)

    def test_definition_sits_between_verdicts_and_output_format(self) -> None:
        prompt = AD.ENTAIL_PROMPT
        verdicts = prompt.index("판정값:")
        definition = prompt.index(CORE_DEFINITION)
        output = prompt.index("strict JSON만 출력하라.")
        self.assertLess(verdicts, definition)
        self.assertLess(definition, output)

    def test_output_format_and_placeholders_are_unchanged(self) -> None:
        prompt = AD.ENTAIL_PROMPT
        self.assertTrue(prompt.startswith("<!-- AUTO-DISTILL-EXTRACTION-V1 -->\n"))
        self.assertEqual(prompt.count(VERDICT_FORMAT), 1)
        # Exactly the two `%s` slots (claim, quotes) and no stray `%`, or the
        # `ENTAIL_PROMPT % (fact, quotes)` call in entail() breaks.
        self.assertEqual(prompt.count("%s"), 2)
        self.assertEqual(prompt.count("%"), 2)
        rendered = prompt % ("CLAIM-SENTINEL", "- QUOTE-SENTINEL")
        self.assertLess(rendered.index("CLAIM-SENTINEL"), rendered.index("QUOTE-SENTINEL"))


class EntailVerdictParsingTest(unittest.TestCase):
    def run_entail(self, verdict_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            cmd, capture = _fake_model(tmp, verdict_text)
            kept, dropped, usage = AD.entail([_item()], cmd, 30)
            sent = capture.read_text(encoding="utf-8")
        return kept, dropped, usage, sent

    def test_model_receives_the_definition(self) -> None:
        _kept, _dropped, _usage, sent = self.run_entail(
            '{"supported":"yes","why":"핵심 일치"}')
        self.assertIn(CORE_DEFINITION, sent)
        self.assertIn("가상 서비스 A는 실패 요청을 재시도한다.", sent)

    def test_yes_verdict_is_kept(self) -> None:
        kept, dropped, usage, _sent = self.run_entail(
            '{"supported":"yes","why":"핵심 일치"}')
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])
        self.assertEqual(usage.get("requests"), 1)

    def test_no_verdict_is_not_entailed_with_reason(self) -> None:
        kept, dropped, _usage, _sent = self.run_entail(
            '{"supported":"no","why":"목표를 완료로 단정"}')
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        item, reason = dropped[0]
        self.assertEqual(reason, "not_entailed")
        self.assertEqual(item["_entail_why"], "목표를 완료로 단정")

    def test_verdict_is_case_and_whitespace_tolerant(self) -> None:
        kept, dropped, _usage, _sent = self.run_entail(
            'verdict: {"supported":" YES ","why":"ok"}')
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_unknown_verdict_value_is_entail_missing(self) -> None:
        kept, dropped, _usage, _sent = self.run_entail(
            '{"supported":"partial","why":"부분 지지"}')
        self.assertEqual(kept, [])
        self.assertEqual([reason for _it, reason in dropped], ["entail_missing"])

    def test_unparseable_answer_fails_closed(self) -> None:
        kept, dropped, _usage, _sent = self.run_entail("판정할 수 없음")
        self.assertEqual(kept, [])
        self.assertEqual([reason for _it, reason in dropped], ["entail_unavailable"])


if __name__ == "__main__":
    unittest.main()
