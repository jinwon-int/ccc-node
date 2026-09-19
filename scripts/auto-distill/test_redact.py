#!/usr/bin/env python3
"""시크릿 마스킹 회귀 — auto-distill.redact() 와 publish_wiki 게이트.

세 가지를 고정한다.

1. 알려진 시크릿 모양은 렌더 시점에 마스킹된다.
2. **두 복사본이 어긋나지 않는다.** auto-distill.py:37 은 "패턴은 wiki-pr-gate 의
   TOKEN_RE / ASSIGN_RE 와 정렬한다"고 약속하지만 코드로 강제된 적이 없어
   실제로 벌어져 있었다. 텍스트 비교는 publish_wiki 가 자기 소스에 리터럴
   `github_pat_` 를 남기지 않으려고 문자열을 쪼개 놓아서 불가능하다. 그러므로
   같은 표본을 양쪽에 먹여 **행동으로** 비교한다.
3. **커밋 SHA 는 살아남는다.** 길이만 보는 포괄 패턴(`[A-Za-z0-9]{40,}` 류)을
   쓰면 안 되는 이유다. 실측: 백필 비교셋 716k자에서 그런 패턴은 165건
   적중했고 전부 커밋 SHA 였다. 증거 인용이 대량 파괴된다.

여기 값은 전부 합성이다. 실제 자격증명을 커밋하지 마라.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from publish_wiki import ASSIGN_RE, TOKEN_RE  # noqa: E402


def _load_auto_distill():
    spec = importlib.util.spec_from_file_location(
        "managed_auto_distill", HERE / "auto-distill.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load managed auto-distill")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTO_DISTILL = _load_auto_distill()

# 합성 시크릿. 각 항목은 (이름, 값).
SECRET_SAMPLES = [
    ("pem", "-----BEGIN RSA PRIVATE KEY-----"),
    ("github_pat", "github" "_pat_" + "A" * 24),
    ("github_classic", "ghp_" + "B" * 36),
    ("openai", "sk-" + "C" * 40),
    ("aws", "AKIA" + "D" * 16),
    ("google", "AIza" + "E" * 35),
    ("slack_token", "xoxb-" + "1" * 24),
    ("telegram", "123456789:" + "F" * 35),
    ("tailscale", "tskey-auth-" + "G" * 20),
    ("gitlab", "glpat-" + "H" * 24),
    ("huggingface", "hf_" + "I" * 34),
    ("npm", "npm_" + "J" * 36),
    ("digitalocean", "dop_v1_" + "a" * 64),
    ("sendgrid", "SG." + "K" * 20 + "." + "L" * 20),
    ("age", "AGE-SECRET-KEY-1" + "M" * 24),
    ("slack_webhook", "hooks.slack.com/services/" + "N" * 28),
    ("jwt", "eyJ" + "O" * 14 + ".eyJ" + "P" * 14 + "."),
    ("url_creds", "https://user:hunter2@example.internal/x"),
    ("kr_mobile", "010-1234-5678"),
]

# `<단어>_<긴 값>` 접두 형태. 구독 서비스 키가 흔히 이 모양이고, _ASSIGN_RE 는
# `api_key:` 처럼 구분자를 요구하므로 이 모양을 놓친다.
#
# 게이트(publish_wiki)에는 지금 넣는다. **마스킹기(auto-distill.redact)에는
# 아직 못 넣는다** — docs/auto-distill.md:84-86 이 "auto-distill.py 는 주석 한 줄만
# 바뀌어도 새 exact-source 평가와 검토된 영수증이 필요하다"고 못박았고, 영수증은
# 로컬에서 만들어 내는 우회 토큰이 아니기 때문이다. 아래 expectedFailure 가
# 그 간극을 눈에 보이게 들고 있는다.
PREFIXED_SAMPLES = [
    ("prefixed_apikey", "apikey_" + "1" * 40),
    ("prefixed_api_key", "api_key_" + "2" * 40),
    ("prefixed_token", "token_" + "3" * 40),
    ("prefixed_secret", "secret_" + "4" * 40),
    ("prefixed_key", "key_" + "5" * 40),
]

# 마스킹되면 안 되는 것 — 전부 정당한 증거에 실제로 등장하는 모양이다.
BENIGN_SAMPLES = [
    ("commit_sha40", "5952c979847985cc95731394514c855fb809e499"),
    ("commit_sha_short", "290fe55f"),
    ("sha256", "e6e5fde445aa5e7eff961df282e52dab59e543e1bc303998d2a1b4c5d6e7f809"),
    ("head_eq_sha", "head=5c615854f683c31347dba85feef85f9322be72fe"),
    ("thread_hash", "c3aa46ca4d920b229cec67ffd1df70c1891ec25143fdadf5f80dd3898c1c18d5"),
    ("issue_ref", "PR #1810 head c8711e0552fa67a421bd41d36ae08ce2b072336e"),
    ("plain_korean", "머지 완료했고 CI 는 14/14 통과했다"),
    ("path", "/root/work/ccc-node/scripts/auto-distill/auto-distill.py:1235"),
    # 짧은 접두 값은 시크릿이 아니다 — 길이 하한이 지켜지는지 본다.
    ("short_prefixed", "token_abc123"),
]


class RedactSecretsTest(unittest.TestCase):
    def test_every_secret_sample_is_masked(self):
        for name, value in SECRET_SAMPLES:
            with self.subTest(sample=name):
                out = AUTO_DISTILL.redact("앞 %s 뒤" % value)
                self.assertNotIn(
                    value, out, "%s 가 마스킹되지 않았다: %r" % (name, out)
                )

    def test_benign_samples_survive(self):
        for name, value in BENIGN_SAMPLES:
            with self.subTest(sample=name):
                out = AUTO_DISTILL.redact("앞 %s 뒤" % value)
                self.assertIn(
                    value, out, "%s 가 잘못 마스킹됐다: %r" % (name, out)
                )

    def test_assign_shape_still_masked(self):
        """`api_key: <값>` 대입 모양은 _ASSIGN_RE 담당. 값만 지우고 키는 남긴다."""
        out = AUTO_DISTILL.redact("api_key: " + "Z" * 32)
        self.assertNotIn("Z" * 32, out)
        self.assertIn("api_key", out)

    @unittest.expectedFailure
    def test_prefixed_shapes_are_not_masked_yet(self):
        """알려진 간극 — 게이트는 잡지만 마스킹기는 아직 못 잡는다.

        이 테스트가 **성공하면 실패로 뒤집힌다**(unexpected success). 즉
        auto-distill.py 에 접두 패턴이 들어오는 순간 여기가 터지므로, 그때
        expectedFailure 를 떼고 PREFIXED_SAMPLES 를 SECRET_SAMPLES 로 합치라는
        신호가 된다. 간극이 조용히 남거나 조용히 메워지는 걸 둘 다 막는다.
        """
        for _name, value in PREFIXED_SAMPLES:
            self.assertNotIn(value, AUTO_DISTILL.redact("앞 %s 뒤" % value))


class PublishWikiGateDriftTest(unittest.TestCase):
    """게이트가 마스킹기와 같은 것을 잡는지 — 37행의 약속을 코드로 강제한다."""

    def test_gate_detects_every_secret_sample(self):
        for name, value in SECRET_SAMPLES + PREFIXED_SAMPLES:
            with self.subTest(sample=name):
                text = "앞 %s 뒤" % value
                hit = bool(TOKEN_RE.search(text) or ASSIGN_RE.search(text))
                self.assertTrue(
                    hit, "publish_wiki 게이트가 %s 를 놓쳤다 (패턴 드리프트)" % name
                )

    def test_gate_does_not_fire_on_benign(self):
        for name, value in BENIGN_SAMPLES:
            with self.subTest(sample=name):
                text = "앞 %s 뒤" % value
                hit = bool(TOKEN_RE.search(text) or ASSIGN_RE.search(text))
                self.assertFalse(
                    hit, "publish_wiki 게이트가 %s 에 오탐했다" % name
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
