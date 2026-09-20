"""Secret masking for ledger payloads — value-level, not just key-name.

``ledger.sanitize`` strips dict entries whose *name* looks secret-bearing. That
misses the commoner leak: a credential sitting inside a string *value*, e.g. a
vendor error message echoing ``https://user:<token>@host``. Any consumer whose
state is free text (error prose, page content, log lines) needs this pass too,
or FW-03 is violated the first time a provider quotes the request URL back.

The pattern block below is a **verbatim copy** of the canonical masker in
``scripts/auto-distill/auto-distill.py`` (lines 45-87). It is duplicated rather
than imported so that jevlib stays importable on its own and a distill-side
import error can never break a gate. ``tests/test_jevlib.py`` asserts the two
pattern strings stay byte-identical, so the copies cannot silently drift.
"""

import re

_B = r"(?<![A-Za-z0-9_])"      # 앞 경계
_E = r"(?![A-Za-z0-9_])"       # 뒤 경계
_TOKEN_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    + r"|" + _B + r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}"
    + r"|" + _B + r"sk-[A-Za-z0-9_-]{32,}"
    + r"|" + _B + r"AKIA[0-9A-Z]{16}" + _E
    + r"|" + _B + r"AIza[0-9A-Za-z_-]{30,}"
    + r"|" + _B + r"xox[baprs]-[0-9A-Za-z-]{20,}"
    # 텔레그램 봇토큰. **순수 소문자 hex 본문을 제외**한다. `1758240000:<sha40>` 은
    # 이 저장소 운영 산문(watermark·커서·상태 덤프)의 평범한 모양인데 여기에
    # 걸려 정당한 증거 인용이 훼손됐다. 봇토큰은 대소문자·밑줄이 섞이고 SHA 는
    # 순수 소문자 hex 라서 이 lookahead 로 갈린다.
    + r"|" + _B + r"[0-9]{8,10}:(?![a-f0-9]{30,}(?![A-Za-z0-9_-]))[A-Za-z0-9_-]{30,}"
    r"|tskey-[a-z]+-[A-Za-z0-9]{10,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    + r"|" + _B + r"hf_[A-Za-z0-9]{30,}"
    # npm 토큰은 36자가 표준이지만 하한만 두어 더 긴 것도 잡는다. 이전의
    # `{36}` + 뒤 경계는 37자짜리를 통째로 놓쳤다.
    + r"|" + _B + r"npm_[A-Za-z0-9]{36,}"
    # dop_v1_ 는 대문자 hex 도 받는다. 소문자만 보던 이전 형태는 대문자를 놓쳤다.
    + r"|" + _B + r"dop_v1_[A-Fa-f0-9]{64,}"
    + r"|" + _B + r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"|AGE-SECRET-KEY-1[A-Z0-9]{20,}"
    r"|hooks\.slack\.com/services/[A-Za-z0-9/_+-]{20,}"
    + r"|" + _B + r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
    r"|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
    # `<다단어>_<32자↑>` 접두 형태. 구독 서비스 키가 흔히 이 모양이고 _ASSIGN_RE 는
    # `api_key:` 처럼 구분자를 요구하므로 놓쳤다. 접두어는 다단어만 받는다 —
    # bare `key_`/`token_`/`secret_` 는 `key_<md5>` 같은 캐시 키와 충돌한다.
    # 길이 하한과 접두어 앵커는 둘 다 필수다: 길이만 보는 포괄 패턴
    # (`[A-Za-z0-9]{40,}`)은 실데이터 716k자에서 165건 적중했고 전부 커밋 SHA 였다.
    #
    # 본문에 `_` 를 허용한다. 실제 발급 키가 `apikey_<36hex>_<64hex>` 처럼
    # 밑줄로 나뉜 다구간이라, 본문을 `[A-Za-z0-9]` 로만 두면 첫 구간에서 멈춰
    # **뒤 64자가 그대로 남았다**(관측: 108자 중 43자만 마스킹). 첫 글자만
    # `_` 를 빼서 `apikey__…` 같은 빈 구간을 배제한다.
    + r"|" + _B + r"(?:apikey|api_key|apitoken|api_token|secret_key|access_token)_[A-Za-z0-9][A-Za-z0-9_]{31,}"
    + r"|" + _B + r"01[016789][-. ]?[0-9]{3,4}[-. ]?[0-9]{4}" + _E + r")")
_ASSIGN_RE = re.compile(
    r"((?:password|passwd|api[_-]?key|secret|access[_-]?token|client[_-]?secret|bearer)"
    r"\s*[:=]\s*[\"']?)([A-Za-z0-9_+/=.-]{16,})", re.I)


def redact_text(text):
    """Mask secret-shaped substrings. Values are never kept, only located."""
    if not isinstance(text, str) or not text:
        return text
    text = _TOKEN_RE.sub("[REDACTED-SECRET]", text)
    text = _ASSIGN_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text
