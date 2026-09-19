#!/usr/bin/env python3
"""시크릿 마스킹 회귀 — auto-distill.redact() 와 publish_wiki 게이트.

네 가지를 고정한다.

1. 알려진 시크릿 모양은 렌더 시점에 마스킹된다.
2. **두 복사본의 차이가 선언된 것뿐이다.** auto-distill.py:37 은 정렬을 약속하지만
   코드로 강제된 적이 없어 실제로 벌어져 있었다(게이트가 10개를 놓쳤다). 표본
   기반 대조는 표본이 우연히 양쪽을 만족하면 차이를 못 본다. 그래서 두 패턴의
   **최상위 대안을 분해해 집합으로 대조**하고, 의도된 차이만 화이트리스트에 둔다.
   화이트리스트에 없는 차이는 실패한다.
3. **커밋 SHA·한글 밀착 토큰은 살아남는다.** 길이만 보는 포괄 패턴을 쓰면 안 되는
   이유: 백필 비교셋 716k자에서 `[A-Za-z0-9]{40,}` 는 165건 적중했고 전부 커밋
   SHA 였다.
4. **게이트 오탐은 발행 중단이다.** `split_document()` 는 fail-closed 이고 이미
   발행된 AUTO.md 에도 돌며(publish_wiki.py:285), `PublishError` 는 11노드 전체
   실행을 중단시킨다(:478). 그래서 오탐 표본이 진탐 표본만큼 중요하다.

여기 값은 전부 합성이다. 실제 자격증명을 커밋하지 마라.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import publish_wiki  # noqa: E402
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


def split_alternatives(pattern: str) -> list[str]:
    """최상위 `|` 로만 쪼갠다.

    `(?:ghp|gho|…)` 처럼 그룹 안에도 `|` 가 있으므로 깊이를 세야 한다. 문자
    클래스 안의 `|` 와 이스케이프도 건너뛴다.
    """
    body = pattern
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    in_class = False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            buf.append(body[i : i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            buf.append(ch)
        elif ch == "[":
            in_class = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "|" and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [a for a in out if a]


# 게이트에만 있는 대안 — 마스킹기는 영수증 제약 때문에 아직 못 받는다.
# docs/auto-distill.md:84-86: auto-distill.py 는 주석 한 줄만 바뀌어도 새
# exact-source 평가와 검토된 영수증이 필요하고, 영수증은 로컬 생성 우회 토큰이
# 아니다. 그래서 게이트 ⊃ 마스킹기 상태가 일시적으로 생긴다.
GATE_ONLY: set[str] = set()

# 게이트가 마스킹기보다 **좁은** 대안 — {마스킹기 형태: 게이트 형태}.
# 게이트는 fail-closed 이고 이미 발행된 문서에도 돌기 때문에, 오탐 비용이
# 마스킹기(값을 가릴 뿐)보다 비대칭적으로 크다.
GATE_NARROWED: dict[str, str] = {}

SECRET_SAMPLES = [
    ("pem", "-----BEGIN RSA PRIVATE KEY-----"),
    ("github_pat", "github" "_pat_" + "A" * 24),
    ("github_classic", "ghp_" + "B" * 36),
    ("openai", "sk-" + "C" * 40),
    ("aws", "AKIA" + "D" * 16),
    ("google", "AIza" + "E" * 35),
    ("slack_token", "xoxb-" + "1" * 24),
    ("telegram", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
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

# 다단어 접두 형태. 구독 서비스 키가 흔히 이 모양이고, _ASSIGN_RE 는
# `api_key:` 처럼 구분자를 요구하므로 접두사로 붙은 경우를 놓친다.
# 이제 마스킹기·게이트 양쪽에 있다.
PREFIXED_SAMPLES = [
    ("prefixed_apikey", "apikey_" + "1" * 40),
    ("prefixed_api_key", "api_key_" + "2" * 40),
    ("prefixed_apitoken", "apitoken_" + "3" * 40),
    ("prefixed_api_token", "api_token_" + "4" * 40),
    ("prefixed_secret_key", "secret_key_" + "5" * 40),
    ("prefixed_access_token", "access_token_" + "6" * 40),
]

# 대입 모양 — _ASSIGN_RE 담당. TOKEN_RE 로는 잡히지 않아야 하므로 게이트에서
# ASSIGN_RE 가 실제로 판정에 기여하는지 확인하는 유일한 표본이다.
ASSIGN_SAMPLES = [
    ("assign_password", "password: " + "Q" * 20),
    ("assign_api_key", "api-key = " + "R" * 24),
    ("assign_bearer", 'bearer: "' + "S" * 32 + '"'),
    ("assign_client_secret", "client_secret=" + "T" * 20),
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
    ("short_prefixed", "apikey_abc123"),
    # 경계 탐침 — {32,} 하한이 실제로 지켜지는지. 31자는 미달, 32자는 적중.
    ("boundary_31", "apikey_" + "7" * 31),
    # 게이트 오탐 실측분 — 이들이 적중하면 11노드 발행이 영구 차단된다.
    ("cache_key_md5", "캐시 key_d41d8cd98f00b204e9800998ecf8427e 삭제"),
    ("token_plus_sha", "token_5952c979847985cc95731394514c855fb809e499"),
    ("secret_bare", "secret_" + "8" * 40),
]

# 과잉 마스킹이 고쳐졌으므로 이제 양쪽 모두 통과해야 한다. 남겨 두는 이유는
# 회귀 방지다 — `1758240000:<sha>` 는 운영 산문의 평범한 모양이다.
# (이전 주석) **선재 결함** — 게이트는 좁혀서 통과하지만 마스킹기는 과잉 마스킹했다.
# `1758240000:<sha>` 는 이 저장소 운영 산문(watermark·커서·상태 덤프)의 평범한
# 모양인데 텔레그램 봇토큰 대안에 걸린다. 인용문이 축자라서 증거가 훼손된다 —
# 길이만 보는 패턴을 반대한 것과 같은 종류의 피해다.
#
# origin/main 에도 있는 결함이며 마스킹기 수정은 영수증 재발급 대상이므로,
# 접두-형태 추가와 **같은 재평가 회차에서 함께** 고쳐야 한다. 그때까지 이
# 목록이 현재 동작을 기록한다.
MASKER_OVERMASKS = [
    ("epoch_plus_sha40",
     "watermark 1758240000:5952c979847985cc95731394514c855fb809e499 까지"),
    ("epoch_plus_sha256",
     "1758240000:e6e5fde445aa5e7eff961df282e52dab59e543e1bc303998d2a1b4c5d6e7f809"),
]


class PatternAlignmentTest(unittest.TestCase):
    """표본이 아니라 패턴 자체를 대조한다 — 우연한 통과가 불가능하다."""

    def test_only_declared_differences_remain(self):
        gate = set(split_alternatives(TOKEN_RE.pattern))
        masker = set(split_alternatives(AUTO_DISTILL._TOKEN_RE.pattern))

        # 게이트에서 선언된 차이를 되돌려 마스킹기 형태로 정규화한다.
        reverse = {v: k for k, v in GATE_NARROWED.items()}
        normalized = {reverse.get(a, a) for a in gate - GATE_ONLY}

        self.assertEqual(
            normalized,
            masker,
            "게이트와 마스킹기의 차이가 선언된 것 밖에 있다.\n"
            "  게이트에만: %s\n  마스킹기에만: %s\n"
            "의도된 차이면 GATE_ONLY / GATE_NARROWED 에 등록하라."
            % (sorted(normalized - masker), sorted(masker - normalized)),
        )

    def test_declared_differences_are_actually_present(self):
        """화이트리스트가 낡아서 조용히 무력해지는 것을 막는다."""
        gate = set(split_alternatives(TOKEN_RE.pattern))
        masker = set(split_alternatives(AUTO_DISTILL._TOKEN_RE.pattern))
        for alt in GATE_ONLY:
            self.assertIn(alt, gate, "GATE_ONLY 항목이 게이트에 없다: %s" % alt)
            self.assertNotIn(alt, masker, "GATE_ONLY 항목이 마스킹기에 이미 있다 — 등록을 지워라")
        for wide, narrow in GATE_NARROWED.items():
            self.assertIn(narrow, gate, "좁힌 형태가 게이트에 없다: %s" % narrow)
            self.assertIn(wide, masker, "넓은 형태가 마스킹기에 없다: %s" % wide)

    def test_assign_re_bodies_agree(self):
        """ASSIGN_RE 는 캡처 그룹 유무만 달라야 한다."""
        gate = ASSIGN_RE.pattern.replace("(?:", "(")
        mask = AUTO_DISTILL._ASSIGN_RE.pattern.replace("(?:", "(")
        self.assertEqual(
            re.sub(r"[()]", "", gate),
            re.sub(r"[()]", "", mask),
            "ASSIGN_RE 본문이 갈라졌다",
        )
        self.assertEqual(ASSIGN_RE.flags, AUTO_DISTILL._ASSIGN_RE.flags)


class RedactSecretsTest(unittest.TestCase):
    def test_every_secret_sample_is_masked(self):
        for name, value in SECRET_SAMPLES:
            with self.subTest(sample=name):
                out = AUTO_DISTILL.redact("앞 %s 뒤" % value)
                self.assertNotIn(value, out, "%s 가 마스킹되지 않았다" % name)

    def test_benign_samples_survive(self):
        for name, value in BENIGN_SAMPLES:
            with self.subTest(sample=name):
                out = AUTO_DISTILL.redact("앞 %s 뒤" % value)
                self.assertIn(value, out, "%s 가 잘못 마스킹됐다: %r" % (name, out))

    def test_assign_shape_masks_value_keeps_key(self):
        out = AUTO_DISTILL.redact("api_key: " + "Z" * 32)
        self.assertNotIn("Z" * 32, out)
        self.assertIn("api_key", out)

    def test_epoch_sha_survives_masking(self):
        """운영 산문 `1758240000:<sha>` 가 살아남는다 (과잉 마스킹 회귀 방지)."""
        for name, value in MASKER_OVERMASKS:
            with self.subTest(sample=name):
                self.assertIn(
                    value,
                    AUTO_DISTILL.redact("앞 %s 뒤" % value),
                    "%s 가 다시 과잉 마스킹된다 — 증거 인용이 훼손된다" % name,
                )

    def test_prefixed_shapes_are_masked(self):
        for name, value in PREFIXED_SAMPLES:
            with self.subTest(sample=name):
                self.assertNotIn(
                    value, AUTO_DISTILL.redact("앞 %s 뒤" % value),
                    "%s 가 마스킹되지 않았다" % name,
                )


class PublishWikiGateTest(unittest.TestCase):
    def _hit(self, text: str) -> bool:
        return bool(TOKEN_RE.search(text) or ASSIGN_RE.search(text))

    def test_gate_detects_token_samples(self):
        for name, value in SECRET_SAMPLES + PREFIXED_SAMPLES:
            with self.subTest(sample=name):
                self.assertTrue(
                    TOKEN_RE.search("앞 %s 뒤" % value),
                    "게이트 TOKEN_RE 가 %s 를 놓쳤다" % name,
                )

    def test_gate_assign_re_carries_its_own_weight(self):
        """ASSIGN_RE 가 판정에 실제로 기여하는지 — TOKEN_RE 로는 안 잡히는 표본으로."""
        for name, value in ASSIGN_SAMPLES:
            with self.subTest(sample=name):
                self.assertIsNone(
                    TOKEN_RE.search(value),
                    "%s 가 TOKEN_RE 에도 걸려 ASSIGN_RE 검증이 무의미해진다" % name,
                )
                self.assertTrue(
                    ASSIGN_RE.search(value), "게이트 ASSIGN_RE 가 %s 를 놓쳤다" % name
                )

    def test_gate_does_not_fire_on_benign(self):
        for name, value in BENIGN_SAMPLES + MASKER_OVERMASKS:
            with self.subTest(sample=name):
                self.assertFalse(
                    self._hit("앞 %s 뒤" % value),
                    "게이트가 %s 에 오탐했다 — 11노드 발행이 중단된다" % name,
                )

    def test_gate_blocks_document_publication(self):
        """패턴이 아니라 실제 게이트 경로가 막는지 확인한다."""
        doc = (
            "# [DOC-auto-dungae] dungae AUTO — 자동 승격 후보 (auto-distill)\n\n"
            "### 항목\n\n키는 apikey_" + "9" * 40 + " 였다\n"
        )
        with self.assertRaises(publish_wiki.PublishError) as ctx:
            publish_wiki.split_document(doc, "dungae", "test")
        self.assertIn("secret-like", str(ctx.exception))
        self.assertNotIn("9" * 40, str(ctx.exception), "예외 메시지가 값을 되비친다")


class HangulAdjacencyTest(unittest.TestCase):
    """한글 조사에 밀착한 토큰도 마스킹된다.

    이전에는 `\\b` 를 써서 전부 빠져나갔다 — `\\b` 는 유니코드 인식이라 한글도
    단어문자이고, 한국어 코퍼스에서 조사 밀착은 예외가 아니라 정상이다.
    지금은 ASCII 전용 lookaround(`_B`/`_E`)를 쓴다.
    """

    HANGUL_GLUED = [
        ("aws_trailing", "AKIAIOSFODNN7EXAMPLE이다"),
        ("ghp_leading", "키는ghp_" + "B" * 36 + "였다"),
        ("hf_leading", "허깅페이스hf_" + "I" * 34),
        ("npm_trailing", "npm_" + "J" * 36 + "였다"),
        ("npm_37chars", "npm_" + "J" * 37),
        ("dop_uppercase_hex", "dop_v1_" + "A" * 64),
    ]

    def test_hangul_glued_tokens_are_masked(self):
        for name, value in self.HANGUL_GLUED:
            with self.subTest(sample=name):
                self.assertNotIn(
                    value, AUTO_DISTILL.redact(value),
                    "%s 가 한글 인접 때문에 빠져나갔다" % name,
                )

    def test_gate_also_catches_hangul_glued(self):
        for name, value in self.HANGUL_GLUED:
            with self.subTest(sample=name):
                self.assertTrue(TOKEN_RE.search(value), "게이트가 %s 를 놓쳤다" % name)

    def test_hangul_prose_survives(self):
        for text in ("머지 완료했고 CI 는 14/14 통과했다",
                     "커밋 5952c979847985cc95731394514c855fb809e499 를 확인했다"):
            with self.subTest(text=text[:20]):
                self.assertEqual(AUTO_DISTILL.redact(text), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
