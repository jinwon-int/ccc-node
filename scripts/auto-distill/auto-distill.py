#!/usr/bin/env python3
"""세션 -> Wiki AUTO.md 자동 승격 (TM-2380 V1 카나리).

This is the ccc-node managed source. Deploy it with
``scripts/install-auto-distill.sh``; do not hand-edit fleet copies.

V1 계약:
  - 입력은 piri 대화형 세션만. codex_exec 로그는 절대 읽지 않는다.
    (~/.codex/sessions 는 distill 파이프라인 자신의 호출 기록이라
     읽는 순간 자기 출력을 원문으로 착각하는 되먹임 루프가 된다.)
  - 워터마크 이후 증가분만 처리한다. 멱등.
  - 후보는 반드시 세션 메시지 id 를 증거로 인용해야 한다.
    인용이 없거나 그 id 가 실제 파일에 없으면 폐기한다 (fail-closed).
  - 정본(ND/LOG/SERVICES) 직행 금지. AUTO.md 전용.
  - V1 은 PR 생성까지. 머지는 사람이 한다.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from model_command import (
    ModelCommandError,
    resolve_explicit_model_command,
    resolve_model_command,
)

# ── 시크릿 마스킹 ────────────────────────────────────────────────────────────
# AUTO.md 에 싣는 인용문은 세션 원문 축자다. 세션에는 명령 출력이 그대로 들어 있어
# 토큰·키가 섞일 수 있다. wiki-pr-gate 가 막아 주긴 하지만 **게이트가 막는 시점엔
# 이미 로컬 커밋과 push 된 브랜치에 값이 들어가 있다.** 그러므로 렌더 시점에
# 마스킹한다. 패턴은 wiki-pr-gate 의 TOKEN_RE / ASSIGN_RE 와 정렬한다.
_TOKEN_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{32,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\bxox[baprs]-[0-9A-Za-z-]{20,}"
    r"|\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}"
    r"|tskey-[a-z]+-[A-Za-z0-9]{10,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|\bhf_[A-Za-z0-9]{30,}"
    r"|\bnpm_[A-Za-z0-9]{36}\b"
    r"|\bdop_v1_[a-f0-9]{64}\b"
    r"|\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"|AGE-SECRET-KEY-1[A-Z0-9]{20,}"
    r"|hooks\.slack\.com/services/[A-Za-z0-9/_+-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
    r"|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
    r"|\b01[016789][-. ]?[0-9]{3,4}[-. ]?[0-9]{4}\b)")
_ASSIGN_RE = re.compile(
    r"((?:password|passwd|api[_-]?key|secret|access[_-]?token|client[_-]?secret|bearer)"
    r"\s*[:=]\s*[\"']?)([A-Za-z0-9_+/=.-]{16,})", re.I)


def redact(text):
    """인용문에서 시크릿 형태를 지운다. 값은 절대 남기지 않고 위치만 표시한다."""
    if not text:
        return text
    text = _TOKEN_RE.sub("[REDACTED-SECRET]", text)
    text = _ASSIGN_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text

HOME = os.path.expanduser("~")
SESS_DIR = os.path.join(HOME, ".piri/agent/sessions")
CC_DIR = os.path.join(HOME, ".claude/projects")
FORBIDDEN = os.path.join(HOME, ".codex/sessions")

# 노드마다 어떤 소스가 실재하는지 다르다 (2026-08-22 플릿 실측).
#   piri 만:        daegyo
#   .claude 만:     gwakga, nosuk, yukson, seoseo
#   둘 다:          sogyo, jingun, dungae, soonwook, bangtong, gongyung
# 어느 쪽이든 codex 로그는 절대 소스가 될 수 없다 (자기호출 되먹임).
SOURCES = {"piri": SESS_DIR, "claude": CC_DIR}
STATE = os.path.join(HOME, ".hermes/state/auto-distill.watermark.json")
AUDIT = os.path.join(HOME, ".hermes/logs/auto-distill-audit.jsonl")

MIN_TEXT = 400          # 이보다 짧은 다이제스트는 건질 게 없다
# 다이제스트 상한(#1295): 초과 시 오래된 앞부분부터 자른다(digest_session 참조).
MAX_DIGEST_BYTES = 400_000
# 현행 파이프라인 버전. 감사로그와 **렌더된 항목 양쪽**에 각인한다.
#   1 = 구조검증만  2 = +함의검증  3 = +격리레인/비용계측
#   4 = +가치판정/인용보수  5 = +정본 중복 대조
# 감사로그에만 박으면 사람 판정(AUTO.md)이 버전 구분 없이 섞여서, 로그 쪽에서
# 막은 혼합 오염이 판정 쪽으로 그대로 되돌아온다 (2026-08-22 83% 사건과 동형).
PIPELINE = 6

# 호스트명 → Family Wiki 노드명. 크론은 `--node` 없이 돌기 때문에 이 표가 없으면
# 호스트명으로 렌더된다. 위키에는 `pages/nodes/vps5` 와 `pages/nodes/yukson` 이
# **둘 다** 있어서, 호스트명으로 올리면 조용히 다른(구) 페이지에 붙는다.
# 판정을 읽는 metrics 는 위키 노드명으로 찾으므로 표본이 통째로 유실된다.
NODE_ALIASES = {
    "vps7": "gwakga",
    "vps5": "yukson",
    "racknerd-167be94": "sogyo",
    "localhost": "daegyo",     # termux 는 호스트명이 일반명이라 특히 위험하다
    "nosuk": "nosuk",
}


def resolve_node():
    """이 노드의 위키 노드명. 환경변수 > 별칭표 > 호스트명 순."""
    env = os.environ.get("CCC_AUTO_NODE")
    if env:
        return env
    h = os.uname().nodename
    return NODE_ALIASES.get(h, h)

CAP_PER_RUN = 5         # 회당 세션 상한


def log_audit(rec):
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(AUDIT, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


AUTO_HEADER = """# [DOC-auto-%(node)s] %(node)s AUTO — 자동 승격 후보 (auto-distill)

> **에이전트 다이제스트** — status: `unverified` · source: `auto-distill` · verified: (미검증)
> - 이 페이지: [TM-2380](/pages/decisions/wiki-auto-promotion-2026-08#tm-2380) 파이프라인이 %(node)s 세션에서 자동 추출한 항목
> - **정본이 아니다.** 여기 있는 항목은 사람이 검토·승격하기 전까지 사실로 인용하지 말 것
> - 인용문은 세션 원문 축자이며 시크릿 형태는 렌더 시점에 마스킹된다([FW-03])
> - 승격 절차: 항목 검토 → 맞으면 해당 정본(ND/LOG/SERVICES)으로 옮기고 여기서 제거
>
> **판정 남기는 법** — 항목의 `**상태**` 를 아래 셋 중 하나로 바꾼다. 이 어휘가
> 곧 지표다(2026-08-22 실측: `fix-citation` 이 없었으면 손실 4건이 `discarded`
> 로 뭉개져 손실률이 0%%로 보였다).
> - `promoted` — 정본으로 승격했다 (= 올릴 만했다)
> - `fix-citation` — 내용은 맞으나 인용이 부실하다 (= 올릴 만한데 게이트가 옳게 막았다)
> - `discarded` — 버렸다 (= 올릴 만하지 않았다)
>
> 지표는 `**파이프라인**` 각인이 같은 항목끼리만 낸다. 구성이 다른 실행을 섞으면
> 지표가 조용히 거짓이 된다.
"""

ITEM_TPL = """
### %(mark)s — %(title)s

- **사실**: %(fact)s
- **분류**: `%(kind)s` · **상태**: `%(status)s` · **키**: `%(key)s` · **파이프라인**: `v%(pipeline)s`
- **출처**: 세션 `%(session)s` · 근거 %(evid)s · %(ts)s
%(whyline)s%(quotes)s"""


_INJECT_RE = re.compile(
    r"\[(?:이전 대화 맥락[^\]]*|Replying to your previous message:.*?)\]", re.S)


def clean_quote(t, limit=260):
    """인용문을 위키에 실을 수 있게 다듬는다.

    세션 전환 시 주입되는 맥락 블록(`[이전 대화 맥락 …]`, `[Replying to …]`)은
    이전 턴의 출력을 통째로 품고 있어, 그대로 실으면 인용이 아니라 중복 사본이
    된다. 근거로서의 가치도 없다(그 텍스트는 이 세션이 만든 사실이 아니다).
    """
    t = (t or "").strip()
    # 맥락 주입으로 시작하는 인용은 뒤따르는 본문 전체가 이전 턴의 사본이다.
    # 마커만 지우면 사본이 그대로 남아 '근거'로 위장하므로 통째로 무효 처리한다.
    body = re.sub(r"^\s*(?:user|assistant|어시스턴트|사용자)\s*:\s*", "", t)
    if re.match(r"^\s*\[(?:이전 대화 맥락|Replying to your previous message)", body):
        return "_(주입된 이전 맥락 — 이 세션의 근거가 아님)_"
    t = _INJECT_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = redact(t)
    if not t:
        return "_(내용 없음)_"
    return (t[:limit] + " …") if len(t) > limit else t


def item_key(it):
    """항목의 안정 식별자. 같은 사실이 재추출돼도 중복 기록되지 않게 한다."""
    basis = (it.get("fact", "") or "") + "|" + ",".join(sorted(it.get("evidence", []) or []))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def render_items(node, kept, quarantined, session, ts):
    """AUTO.md 에 붙일 마크다운 조각을 만든다.

    승격 후보와 검토 대기를 한 페이지에 두되 마킹으로 구분한다. 격리분을 별도
    페이지로 빼지 않는 이유: 검토자가 두 곳을 보게 만들면 한쪽은 안 보게 된다.
    """
    out = []
    for it, status, mark, why in (
            [(x, "unverified", "✅ 승격 후보", "") for x in kept]
            + [(x, "needs-review", "🔍 검토 대기", y) for x, y in quarantined]):
        quotes = ""
        for t in (it.get("_evidence_text") or []):
            quotes += "\n  > %s\n" % clean_quote(t)
        suspect = it.get("_canon_suspect")
        whyline = ""
        if suspect:
            whyline += ("- **정본 확인 필요**: 비슷한 기록이 있을 수 있음 → `%s`"
                        " (판정기가 근거 문장을 대지 못해 버리지 않고 표시함)\n"
                        % redact(suspect))
        if why:
            whyline = "- **검토 사유**: `%s` — %s\n" % (
                why, redact(it.get("_entail_why", "") or "인용이 주장을 뒷받침하는지 불확실"))
        out.append(ITEM_TPL % {
            "mark": mark, "title": redact(it.get("title", "") or "(제목 없음)"),
            "fact": redact(it.get("fact", "") or ""),
            "kind": it.get("kind", "?") or "?", "status": status,
            "key": item_key(it), "session": session, "pipeline": PIPELINE,
            "evid": ", ".join("`%s`" % e for e in (it.get("evidence") or [])) or "—",
            "ts": ts, "whyline": whyline, "quotes": quotes,
        })
    return "".join(out)


def merge_auto_md(path, node, chunk):
    """AUTO.md 에 멱등 병합. 이미 있는 키는 다시 쓰지 않는다.

    크론이 30분마다 도는데 워터마크만으로는 같은 사실의 재추출을 막지 못한다
    (세션이 더 자라면 그 세션을 다시 읽는다). 키 기준으로 걸러야 페이지가
    같은 항목으로 부풀지 않는다.
    """
    existing = ""
    if os.path.exists(path):
        existing = open(path, errors="replace").read()
    else:
        existing = AUTO_HEADER % {"node": node}
    have = set(re.findall(r"\*\*키\*\*: `([0-9a-f]{12})`", existing))
    kept_chunk, added, skipped = [], 0, 0
    for block in re.split(r"(?=\n### )", chunk):
        if not block.strip():
            continue
        m = re.search(r"\*\*키\*\*: `([0-9a-f]{12})`", block)
        if m and m.group(1) in have:
            skipped += 1
            continue
        kept_chunk.append(block)
        added += 1
    if kept_chunk:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(existing.rstrip("\n") + "\n" + "".join(kept_chunk).rstrip("\n") + "\n")
    return added, skipped


def write_quarantine(path, rec):
    """함의 미통과 항목을 검토 레인에 남긴다 (append-only).

    폐기가 아니라 격리인 이유: 판정기는 노골적인 환각·무관·모순은 확실히 잡지만
    경계 사례에서 회차마다 판정이 흔들린다(2026-08-22 실측 25%). 자동 판정 하나로
    사실을 소멸시키면 그 흔들림이 곧 조용한 유실이 된다. TM-2380의 원칙도
    '정본 직행 금지 + unverified 격리'이지 삭제가 아니다.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


SELF_MARK = "AUTO-DISTILL-EXTRACTION-V1"
# 마커 도입(2026-08-22) 이전에 생성된 자기호출 세션용 fallback.
SELF_MARK_LEGACY = "--- 발췌 시작 ---"


def iter_messages(path, since_line=0):
    """세션 파일에서 (lineno, id, role, text) 를 생성한다.

    두 스키마를 지원한다:
      - piri:        {"type":"message", "id":..., "message":{"role","content"}}
      - Claude Code: {"type":"user"|"assistant", "uuid":..., "message":{...}}

    since_line: 이 줄 번호 이하는 **json.loads 전에** 건너뛴다. 워터마크
    증가분만 필요할 때 이미 처리한 구간을 다시 파싱하는 비용을 없앤다
    (예전에는 전 줄을 파싱하고 호출부가 n <= since_line 을 버렸다).

    INV-2 주의: 스키마 분기를 빠뜨리면 이 함수가 조용히 0건을 내고, 호출부는
    그것을 "건질 게 없는 세션"으로 오해한다. 그래서 인식된 스키마를 세어
    호출부가 '한 줄도 인식 못 함'과 '내용이 없음'을 구분할 수 있게 한다.
    since_line > 0 은 이전 실행이 스키마를 인식하고 남긴 워터마크에서만 오므로
    (lines==0 이면 호출부가 0 을 넘긴다), 건너뛴 구간은 인식된 것으로 친다 —
    증가분이 비어 있을 때 too_small 이 unknown_schema 로 둔갑하지 않게.
    """
    seen_schema = since_line > 0
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if n <= since_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            ty = o.get("type")
            if ty == "message":                       # piri
                m = o.get("message", {}) or {}
                role, mid = m.get("role"), (o.get("id") or "")[:8]
                c = m.get("content")
            elif ty in ("user", "assistant"):          # Claude Code
                m = o.get("message", {}) or {}
                role, mid = (m.get("role") or ty), (o.get("uuid") or "")[:8]
                c = m.get("content")
            elif ty == "response_item":                # Codex CLI rollout (#1295)
                p = o.get("payload") or {}
                if p.get("type") != "message":
                    continue
                role, mid = p.get("role"), (p.get("id") or "")[:8]
                c = p.get("content")
                # codex user 턴에는 도구 맥락/시스템 프리앰블이 섞여 든다
                # (실측: 166 user 중 ~80건이 [/#/< 시작 블록, 오너 발화는 아님).
                # 이 노드의 오너 발화는 한국어 대화체라 세 패턴과 충돌하지
                # 않는다. assistant 응답은 그대로 유지한다.
                if role == "user":
                    _c0 = c[0].get("text", "").lstrip()[:1] \
                        if isinstance(c, list) and c and isinstance(c[0], dict) \
                        else str(c or "").lstrip()[:1]
                    if _c0 in ("[", "#", "<"):
                        continue
            else:
                continue
            if role not in ("user", "assistant"):
                continue
            seen_schema = True
            if isinstance(c, list):
                # thinking / tool_use / tool_result 는 버린다
                # (codex rollout 은 input_text/output_text 만 본문으로 취급)
                txt = "".join(p.get("text", "") for p in c
                              if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
            else:
                txt = str(c or "")
            txt = txt.strip()
            if not txt:
                continue
            yield n, mid, role, txt
    iter_messages.last_schema_ok = seen_schema


def is_self_call(path, max_bytes=8000):
    """추출기 자신의 호출로 생성된 세션인가.

    INV-2 2차 방어. 첫 user 메시지 머리에 추출 프롬프트 마커가 있으면 자기호출이다.
    """
    try:
        for _n, _mid, role, txt in iter_messages(path):
            if role != "user":
                continue
            head = txt[:max_bytes]
            return SELF_MARK in head or SELF_MARK_LEGACY in head
    except Exception:
        return False
    return False


def load_watermark():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_watermark(wm):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(wm, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def digest_session(path, since_line=0):
    """세션 파일에서 user/assistant 텍스트만 뽑는다.

    thinking / toolCall / toolResult 는 버린다. 실측 3.2MB -> 126KB (3%).
    각 줄에 메시지 id 를 붙여 모델이 인용할 앵커로 쓴다.
    """
    # n 을 since_line 에서 시작: 증가분이 비어도 워터마크가 0 으로 퇴행해
    # 다음 회차가 전체를 재처리하는 일이 없게 한다 (append-only 파일에서
    # 예전 동작과 같은 값 — 마지막 yield 줄 == 직전 워터마크).
    kept, ids, n = [], {}, since_line
    anon = 0
    for n_, mid, role, txt in iter_messages(path, since_line=since_line):
        n = n_
        if not mid:
            # 앵커 없는 줄은 증거로 인용될 수 없다. 파일 내 안정적인 대체 id를 준다.
            anon += 1
            mid = ("L%07d" % n_)[:8]
        # 함의 검증기가 읽을 원문. 160자로 자르면 판정기가 잘린 인용을 보고
        # 판단하게 되므로 넉넉히 보관하고, 사람이 보는 출력에서만 자른다.
        ids[mid] = "%s: %s" % (role, txt[:1200].replace("\n", " "))
        kept.append("[%s] %s: %s" % (mid, role, txt))
    # Codex 롤아웃은 시스템 프리앰블/도구 맥락이 user 턴으로 섞여 들어와
    # 다이제스트가 600KB+ 로 커질 수 있다(#1295 실측 625KB). piri/claude 실측은
    # ~126KB. 상한을 넘으면 **오래된 앞부분부터** 자른다 — 대화 끝이 최신 사실
    # 이고, 증거 앤커(ids)도 남은 줄만 유지해 끊긴 인용을 막는다.
    if len("\n".join(kept)) > MAX_DIGEST_BYTES:
        kept_bytes, cut = 0, len(kept)
        for i, chunk in enumerate(kept):
            kept_bytes += len(chunk) + 1
            if kept_bytes > MAX_DIGEST_BYTES:
                cut = i
                break
        kept = kept[cut:]
        kept_ids = {m.group(1) for c in kept for m in [re.match(r"\[([^\]]+)\]", c)] if m}
        ids = {k: v for k, v in ids.items() if k in kept_ids}
    digest_session.last_anon = anon
    return "\n".join(kept), ids, n


PROMPT = """<!-- AUTO-DISTILL-EXTRACTION-V1 -->
다음은 에이전트 작업 세션의 발췌다. 각 줄은 [id] role: 내용 형식이다.

이 중에서 **세션이 끝난 뒤에도 팀에 지속적으로 가치 있는 사실**만 골라라.

반드시 지킬 것:
- 각 항목은 근거가 된 줄의 [id]를 evidence 배열에 넣어라. 추측으로 id를 만들지 마라.
- **fact 에 쓴 모든 내용이 네가 인용한 줄 안에 있어야 한다.** 수치·고유명·결론을 쓰려면
  그 수치가 실제로 적힌 줄을 전부 인용하라. 인용에 없는 것은 fact 에서 빼라.
  여러 줄에 흩어져 있으면 그 줄들을 모두 evidence 에 넣어라.
- 발췌에 명시적으로 없는 내용은 쓰지 마라. 추론·일반론·요약적 감상 금지.
- 일회성 잡담, 진행중 상태, 이미 해결된 임시 오류는 제외하라.
- 토큰·키·비밀번호·전화번호 등 비밀 값은 절대 쓰지 마라. 위치와 취급 규칙만.
- 건질 게 없으면 빈 배열을 반환하라. 억지로 채우지 마라.

strict JSON만 출력하라. 형식:
{"items":[{"title":"짧은 제목","fact":"한두 문장 사실","evidence":["id1","id2"],"kind":"decision|config|incident|runbook"}]}

--- 발췌 시작 ---
%s
--- 발췌 끝 ---"""


def extract(digest, model_cmd, timeout):
    return extract_json(PROMPT % digest, model_cmd, timeout)


def unwrap_claude_envelope(out):
    """Claude Code `--output-format json` 봉투에서 (사용량, 실제 응답) 을 꺼낸다.

    piri 는 사용량을 `PIRI_USAGE=` 줄로 내보내지만 claude 는 내보내지 않는다.
    그래서 claude 기반 노드에서는 비용이 0으로 보고됐다 (2026-08-22 실측).
    키 이름은 piri 쪽에 맞춰 정규화해 회계를 한 곳으로 모은다.

    봉투가 아니면 (None, None) 을 돌려 호출부가 기존 경로를 쓰게 한다.
    """
    try:
        env = json.loads(out)
    except Exception:
        return None, None
    if not isinstance(env, dict) or "result" not in env or env.get("type") != "result":
        return None, None
    u = env.get("usage") or {}
    inp = u.get("input_tokens", 0) or 0
    outp = u.get("output_tokens", 0) or 0
    cr = u.get("cache_read_input_tokens", 0) or 0
    cw = u.get("cache_creation_input_tokens", 0) or 0
    usage = {
        "requests": 1,
        "inputTokens": inp, "outputTokens": outp,
        "cacheReadTokens": cr, "cacheWriteTokens": cw,
        "totalTokens": inp + outp + cr + cw,
        # 캐시 생성분 때문에 사소한 호출도 비용이 0이 아니다. 반드시 싣는다.
        "costUsd": env.get("total_cost_usd", 0) or 0,
        "models": sorted((env.get("modelUsage") or {}).keys()),
    }
    return usage, str(env.get("result") or "")


# 리졸브된 엔진 전용 환경(#1295): main 에서 resolve_model_command() 결과로
# 채우고 extract_json 이 자식 환경에 merge 한다. codex 엔진의 CODEX_HOME
# 격리가 여기를 거치지 않으면(초기화 누락) 추출 세션이 원본 ~/.codex/sessions
# 에 쌓여 자기호출 되먹임이 재발한다 — seoseo 실측으로 잡은 결함이다.
ENGINE_ENV = {}


def extract_json(prompt, model_cmd, timeout):
    """모델을 호출해 strict JSON 하나를 받아온다. 추출·함의검증 공용."""
    child_env = os.environ.copy()
    # Engine-specific overrides (codex: CODEX_HOME isolation — see
    # model_command.codex_scratch_home).
    if ENGINE_ENV:
        child_env.update(ENGINE_ENV)
    # Every extractor backend receives the legacy Claude-hook recursion guard.
    # Piri normally creates no Claude session, but carrying the guard keeps a
    # wrapper or future backend from re-entering SessionStart/End distill.
    child_env["CLAUDE_DISTILL_INFLIGHT"] = "1"
    child_env["CCC_AUTO_DISTILL_INFLIGHT"] = "1"
    try:
        p = subprocess.run(model_cmd, input=prompt, capture_output=True,
                           text=True, timeout=timeout, env=child_env)
    except subprocess.TimeoutExpired:
        return None, ("timeout", {}, "")
    except OSError as exc:
        # Body-free and path-free: command provenance was already recorded by
        # the engine_selected audit event.
        return None, ("model_spawn_error:%s" % type(exc).__name__, {}, "")
    if p.returncode != 0:
        return None, ("model_exit_%d: %s" % (p.returncode, p.stderr.strip()[:200]), {}, "")
    out = p.stdout
    usage = {}
    for line in (p.stdout + "\n" + (p.stderr or "")).splitlines():
        if line.startswith("PIRI_USAGE="):
            try:
                usage = json.loads(line.split("=", 1)[1])
            except Exception:
                pass
    out = "\n".join(
        line for line in out.splitlines() if not line.startswith("PIRI_USAGE=")
    ).strip()
    # Claude Code `--output-format json` 봉투. piri 의 PIRI_USAGE 와 달리 사용량이
    # stdout JSON 안에 들어오므로 여기서 벗겨내지 않으면 비용이 0으로 보고된다.
    env_usage, inner = unwrap_claude_envelope(out)
    if inner is not None:
        usage, out = env_usage, inner
    s, e = out.find("{"), out.rfind("}")
    if s < 0 or e <= s:
        return None, ("no_json", usage, out[:4000])
    try:
        return json.loads(out[s:e + 1]), (None, usage, out[:4000])
    except Exception as ex:
        return None, ("bad_json: %s" % ex, usage, out[:4000])


ENTAIL_PROMPT = """<!-- AUTO-DISTILL-EXTRACTION-V1 -->
아래 "주장"이 아래 "인용문"에 의해 뒷받침되는지 판정하라.

오직 인용문 안에 있는 내용만 근거로 삼아라. 세상 지식이나 그럴듯함으로 판단하지 마라.

판정값:
- "yes" — 인용문이 주장의 핵심 내용을 뒷받침한다.
          표현이 다르거나 주장이 인용문을 요약한 것이어도 핵심이 인용문에 있으면 yes다.
- "no"  — 인용문이 주장과 무관하거나, 주장과 모순되거나,
          주장의 핵심이 인용문에 아예 없는 추론·창작이다.

strict JSON만 출력하라. 형식:
{"supported":"yes|no","why":"15자 이내 사유"}

--- 주장 ---
%s

--- 인용문 ---
%s"""


VALUE_PROMPT = """<!-- AUTO-DISTILL-EXTRACTION-V1 -->
아래 항목이 **세션이 끝난 뒤에도 팀에 지속 가치가 있는 사실**인지 판정하라.

가치 **없음**으로 볼 것:
- 일회성 검증 이벤트 ("그때 그 수치가 맞았다", "테스트가 통과했다")
- 이미 문서로 정해져 있는 규약·설계를 그대로 되풀이한 것
- 진행 중 상태의 스냅샷 (곧 낡는다)
- 그 세션 안에서만 의미 있는 임시 상황

가치 **있음**으로 볼 것:
- 시스템의 제약·결함·실패 양상과 그 원인
- 결정과 그 근거, 하지 않기로 한 것
- 재현 가능한 절차, 운영에 쓰이는 수치·경로·설정
- 나중에 같은 실수를 막아 줄 교훈

strict JSON만 출력하라. 형식:
{"valuable":"yes|no","why":"15자 이내 사유"}

--- 항목 ---
제목: %s
사실: %s"""


REPAIR_PROMPT = """<!-- AUTO-DISTILL-EXTRACTION-V1 -->
아래 "주장"이 "인용문"의 범위를 넘어섰다는 판정을 받았다. 둘 중 하나로 고쳐라.

(A) 발췌에 주장을 뒷받침하는 줄이 더 있으면 그 [id]를 evidence 에 **추가**한다.
(B) 그런 줄이 없으면 **주장을 인용문이 실제로 뒷받침하는 범위로 좁힌다.**
    좁힐 때 인용에 없는 수치·고유명은 지운다. 사실을 지어내지 마라.

좁혀서 남는 게 없으면 fact 를 빈 문자열로 두어라.

strict JSON만 출력하라. 형식:
{"fact":"고친 사실","evidence":["id1","id2"]}

--- 원래 주장 ---
%s

--- 현재 인용문 ---
%s

--- 발췌 (여기 있는 [id] 만 인용 가능) ---
%s"""


CANON_PROMPT = """아래 "주장"이 Family Wiki **정본 발췌**에 이미 기록돼 있는지 판정하라.

주장:
%s

정본 발췌:
%s

기준:
- 같은 사실이 이미 있으면(표현이 달라도) duplicate = yes
- 발췌가 주장보다 **더 최신이라 주장이 낡았다면** 그것도 yes (낡은 사실을 다시 올리면 정본이 흔들린다)
- 발췌가 **주제·이슈번호·파일명만 같고** 그 사실 자체는 없으면 **no** — 이 착각이 가장 흔한 오판이다
- 발췌가 비었으면 no

**yes 로 판정하려면 `quote` 에 그 사실을 담고 있는 발췌 문장을 그대로 옮겨야 한다.**
옮길 문장을 못 고르겠으면 그것은 중복이 아니다 — 애매하면 no 로 간다. 여기서
잘못 yes 를 내면 정본에 없는 지식이 조용히 사라지고, 잘못 no 를 내면 사람이
검토 단계에서 거른다. 두 오류의 값이 다르다.

JSON만: {"duplicate":"yes|no","where":"<yes면 경로#섹션>","quote":"<yes면 발췌 원문 한 문장>"}
"""


# 정본 검색에서 반드시 빼야 하는 경로. AUTO.md 는 이 파이프라인이 만든
# **스테이징 페이지**라 후보 텍스트가 그대로 들어 있다. 배제하지 않으면 모든
# 항목이 자기 자신에 매칭돼 전량 '중복'으로 기각된다 (2026-08-22 판정자 6명이
# 수동으로 이 히트를 걸러냈고, 자동화하면 그대로 밟는다).
CANON_EXCLUDE = re.compile(r"pages/nodes/[^/]+/AUTO\.md")



_FOCUS_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}|[가-힣]{2,}|\d+[가-힣]+")


def _focus_needles(text):
    """창 선정용 표식. TOKEN_RES + 한/영 단어. 검색용이 아니라 점수용."""
    needles, seen = [], set()
    for rx in TOKEN_RES:
        for m in rx.finditer(text or ""):
            t = m.group(1)
            key = (t or "").lower()
            if t and key not in seen:
                seen.add(key)
                needles.append(t)
    for w in _FOCUS_WORD_RE.findall(text or ""):
        key = w.lower()
        if key not in seen:
            seen.add(key)
            needles.append(w)
    return needles


def _needle_score(text, needles):
    return sum(1 for t in needles if t and t in (text or ""))


def _window_body(sec_lines, cap, focus="", match_idx=0):
    """긴 섹션에서 주장과 겹치는 cap 크기 연속 창.

    앞 cap 에 주장 표식이 이미 있으면 그것을 유지(①에서 6/12 뒤집힌
    행동 보존). 앞이 비고 뒤쪽 창 점수가 엄격히 높을 때만 창을 옮긴다.
    cap 을 높이지 않는다 — 발췌가 길어지면 판정기가 과감해진다(TM-2380).
    """
    body = "".join(sec_lines).strip()
    if len(body) <= cap:
        return body
    head = body[:cap]
    needles = _focus_needles(focus) if focus else []
    head_score = _needle_score(head, needles)
    n = len(sec_lines)
    if n == 0:
        return ""
    match_idx = max(0, min(n - 1, int(match_idx or 0)))
    scores = [_needle_score(ln, needles) for ln in sec_lines] if needles else [0] * n
    if needles and max(scores) > 0:
        best_i = max(range(n), key=lambda i: (scores[i], -abs(i - match_idx)))
    else:
        best_i = match_idx

    lo = hi = best_i
    used = len(sec_lines[best_i])
    while True:
        left = len(sec_lines[lo - 1]) if lo > 0 else None
        right = len(sec_lines[hi + 1]) if hi + 1 < n else None
        cands = []
        if left is not None and used + left <= cap:
            cands.append((-1, scores[lo - 1], left))
        if right is not None and used + right <= cap:
            cands.append((1, scores[hi + 1], right))
        if not cands:
            break
        side, _, sz = max(cands, key=lambda x: (x[1], 1 if x[0] < 0 else 0))
        if side < 0:
            lo -= 1
        else:
            hi += 1
        used += sz
    chunk = "".join(sec_lines[lo:hi + 1]).strip()
    if lo > 0:
        heading = sec_lines[0].rstrip()
        glued = heading + "\n…\n" + chunk
        if len(glued) > cap:
            room = cap - len(heading) - 3
            chunk = heading + "\n…" + (chunk[-room:] if room > 0 else "")
            if len(chunk) > cap:
                chunk = chunk[:cap].rstrip() + "…"
        else:
            chunk = glued
    elif len(chunk) > cap:
        chunk = chunk[:cap].rstrip() + "…"
    foc_score = _needle_score(chunk, needles)
    if needles and foc_score > head_score:
        return chunk
    return head.rstrip() + "…"


# section_body 는 항목·토큰마다 같은 위키 파일을 다시 readlines 했다.
# 위키 캐시는 회차 시작에 한 번만 동기화되므로 실행 중에는 불변 — 실행 단위
# 캐시로 중복 읽기를 없앤다. 크기 상한(FIFO)으로 긴 실행의 메모리를 묶는다.
_SECTION_LINES_CACHE = {}      # path -> readlines() 결과 (실패는 None 으로 캐시)
_SECTION_LINES_CACHE_MAX = 256


def _section_lines(path):
    if path in _SECTION_LINES_CACHE:
        return _SECTION_LINES_CACHE[path]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        lines = None
    if len(_SECTION_LINES_CACHE) >= _SECTION_LINES_CACHE_MAX:
        _SECTION_LINES_CACHE.pop(next(iter(_SECTION_LINES_CACHE)))
    _SECTION_LINES_CACHE[path] = lines
    return lines


def section_body(path, lineno=None, needle=None, cap=1500, focus=""):
    """매칭 라인이 속한 ## 섹션 본문 (heading → 다음 heading 직전).

    정본 대조가 제목 한 줄만 보면 인용을 못 대서 keep 으로 흘린다
    (TM-2380 인수인계 실측: 주소 채널 1/12). 본문을 넘기면 판정기가
    quote 를 고를 수 있다.

    섹션이 cap 를 넘으면 앞 cap 만 자른다 — 주소 채널이 heading 을 먼저
    주므로 긴 로그(yukson-15 = 5213자) 의 뒤쪽 사실이 잘린다(실측: 남은 keep 6건
    전부 같은 섹션). focus(항목 사실문)가 있고 뒤쪽 창 점수가 더 높으면
    그 창을 넘긴다. cap 자체는 높이지 않는다.
    """
    lines = _section_lines(path)
    if not lines:
        return ""
    idx = None
    if lineno is not None:
        try:
            idx = max(0, min(len(lines) - 1, int(lineno) - 1))
        except (TypeError, ValueError):
            idx = None
    if idx is None and needle:
        for i, line in enumerate(lines):
            if needle in line:
                idx = i
                break
    if idx is None:
        return ""
    start = 0
    for i in range(idx, -1, -1):
        if lines[i].startswith("## "):
            start = i
            break
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    sec = lines[start:end]
    return _window_body(sec, cap, focus=focus, match_idx=idx - start)


WIKI_CACHE = os.path.join(HOME, ".wiki-agent/wiki-cache/pages")

# 리터럴 대조에 쓸 만한 "고유 표식". 산문은 표현이 갈리지만 이런 토큰은 그대로
# 옮겨 적히므로, 임베딩 없이도 같은 사실을 가리킨다.
TOKEN_RES = [
    # 위키 ID·커밋 sha 는 특정 기록 하나를 가리키므로 가장 안전하다.
    re.compile(r"\b((?:TM|ND|LOG|RB|DOC|INV|FW|SV|OWN)-[0-9A-Za-z-]+)"),
    re.compile(r"\b([0-9a-f]{8,40})\b"),
    # 아래는 **넓힌 표식**(2026-08-22 2차). 좁힌 토큰만으로는 위키 ID도 sha 도 없는
    # 기술 항목을 하나도 못 잡았다(실측: 정본이 캐시에 있는 상태에서 12건 중 0건).
    # 넓히면 주제만 같은 문서를 물어올 위험이 있으나, 그건 **인용 강제**가 막는다 —
    # 근거 문장을 못 대면 폐기가 아니라 '정본 확인 필요' 표시로 빠진다.
    re.compile(r"`([^`\s]{6,60})`"),                          # 백틱 식별자
    # 긴 **식별자** — 평범한 영어 단어(observation, primitives)가 걸리면 주제만 같은
    # 문서를 잔뜩 물어오므로, 코드 식별자의 표식(중간 대문자·밑줄·숫자)을 요구한다.
    # 길이는 정규식 lookbehind 로 재면 **토큰 길이가 아니라 앞 문자 수**를 재게 되어
    # 뒤쪽의 짧은 토큰이 통과한다(실측: `PR` 이 잡혔다). 길이는 코드에서 건다.
    # Consume the first required marker with disjoint character classes. The
    # previous overlapping ``*``/``+`` groups could backtrack exponentially
    # on long digit runs (CodeQL py/redos, #1257).
    re.compile(r"\b([A-Za-z][a-z]*[A-Z0-9_][A-Za-z0-9_]*(?:\.[a-z]{2,4})?)\b"),
    re.compile(r"(?:PR\s*)?(#\d{3,6})\b"),                    # PR/이슈 번호
]


def _literal_tokens(fact):
    """fact 에서 리터럴 대조용 토큰을 뽑는다 (literal_hits 의 선정 규칙 그대로)."""
    toks = []
    for rx in TOKEN_RES:
        for m in rx.finditer(fact or ""):
            t = m.group(1)
            # 짧은 토큰은 표식 구실을 못 한다(`PR`, `id`). 길이는 여기서 건다 —
            # 정규식 안에서 재면 위치에 따라 잘못 걸린다.
            if len(t) < 6 and not t.startswith("#"):
                continue
            if t and t.lower() not in ("true", "false", "null", "none") and t not in toks:
                toks.append(t)
    return toks


def _collect_literal_tokens(items):
    """실행 항목 전체의 리터럴 토큰 합집합. 항목별 선정 순서(≤10개)를 보존한다."""
    seen, out = set(), []
    for it in items:
        for t in _literal_tokens(it.get("fact", "") or "")[:10]:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def build_literal_index(tokens, timeout=60):
    """토큰 전부를 grep **한 번**으로 찾는다: token -> [(path, lineno, line)].

    예전에는 항목마다 토큰(≤10)별 `grep -rIl` + 히트 파일당 `grep -n -m1` 을
    돌려 회차당 수십~수백 프로세스가 떴다. 패턴 파일 하나로 트리를 한 번만
    훑고, 조회는 인메모리로 한다. grep -r 의 트리 순회 순서는 패턴 수와
    무관하므로 토큰별 (파일 순서, 파일당 첫 매치 줄) 이 기존과 동일하다.

    -o 를 쓰지 않는 이유: 겹치는 토큰(`TM-2380` ⊂ `TM-2380-foo`)이 서로의
    매치를 가리고, 발췌 실패 시 대체 발췌로 쓸 매치 줄 원문도 사라진다.
    매치 줄 전체를 받아 파이썬 부분문자열 검사로 토큰에 배정하면 grep 을
    토큰별로 돌린 것과 같은 결과가 나온다.
    """
    index = {t: [] for t in tokens}
    if not tokens or not os.path.isdir(WIKI_CACHE):
        return index
    import tempfile
    pats = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pats", delete=False) as pf:
            pf.write("\n".join(tokens) + "\n")
            pats = pf.name
        r = subprocess.run(
            ["grep", "-rInF", "--include=*.md", "-f", pats, WIKI_CACHE],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return index          # 실패해도 열어 둔다 — 대조 불가는 폐기가 아니다
    finally:
        if pats:
            try:
                os.unlink(pats)
            except OSError:
                pass
    seen = set()   # (token, path) — 파일당 첫 매치만 담는다 (grep -n -m1 과 동일)
    for line in r.stdout.splitlines():
        try:
            path, no, text = line.split(":", 2)
        except ValueError:
            continue
        if not no.isdigit():
            continue
        for t in tokens:
            if t in text and (t, path) not in seen:
                seen.add((t, path))
                index[t].append((path, int(no), text))
    return index


def literal_hits(fact, limit=3, index=None):
    """Wiki 캐시에서 **리터럴**로 같은 표식을 찾는다.

    의미검색 인덱스는 임베딩 빌드 시점에 묶인다. 그런데 이 파이프라인의 최악
    사례는 정확히 그 지연 구간에 있다 — 세션이 위키에 기록하고 30분 뒤 크론이
    그 세션을 증류하는데, 인덱스는 몇 시간 전 것이다(2026-08-22 실측: 방금 머지한
    LOG를 정본 대조가 못 보고 다시 후보로 올렸다).

    캐시 파일은 rsync 로 오므로 임베딩 없이 즉시 최신이다. 의미검색이 못 보는
    지연 구간과, 표현이 달라 유사도가 낮은 경우를 리터럴이 메운다.

    index: build_literal_index() 가 만든 실행 단위 인덱스. 넘기면 grep 없이
    조회만 한다 (canon_dedup 이 항목 루프 앞에서 한 번 만들어 재사용).
    """
    if not os.path.isdir(WIKI_CACHE):
        return []
    toks = _literal_tokens(fact)[:10]
    if not toks:
        return []
    if index is None:
        index = build_literal_index(toks)
    out = []
    for t in toks:
        for path, lineno, line in index.get(t, []):
            if CANON_EXCLUDE.search(path):     # 자기 자신(스테이징)은 정본이 아니다
                continue
            rel = os.path.relpath(path, os.path.dirname(WIKI_CACHE))
            excerpt = section_body(path, lineno=lineno, needle=t, focus=fact) or (line.strip()[:400] if line else "")
            if excerpt:
                out.append("%s :: [리터럴 `%s`] %s" % (rel, t, excerpt))
            if len(out) >= limit:
                return out
    return out


# 세션이 스스로 밝힌 **정본 주소**. 세션은 위키에 기록하면서 그 LOG-ID·PR 번호를
# 대화에 남긴다 — 실측 2026-08-22: 검색으로 0~2/12 밖에 못 찾던 중복의 정본 주소가
# 세션 원문에 **12/12** 들어 있었다. 유사도로 헤맬 일이 아니라 주소를 읽으면 된다.
# (이 보편성은 앞서 '기록 신호로 억제하자'는 안이 21/21 양성이라 폐기된 바로 그
#  성질이다. 억제 신호로는 변별력이 0이지만, 주소로는 그게 장점이다.)
SELF_ADDR_RE = re.compile(r"\b((?:LOG|TM|ND|RB|SV|DOC)-[0-9A-Za-z-]{4,})", re.I)


def self_canon_addrs(digest, limit=6):
    """세션이 밝힌 정본 주소의 캐시 위치. 발췌는 항목 사실문에 맞춰 따로 한다."""
    if not os.path.isdir(WIKI_CACHE):
        return []
    tags, seen = [], set()
    for m in SELF_ADDR_RE.finditer(digest or ""):
        t = m.group(1)
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        tags.append(t)
    out = []
    for t in tags[:limit]:
        try:
            r = subprocess.run(["grep", "-rIn", "--include=*.md", "-F", "-i", "-e", t, WIKI_CACHE],
                               capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        hits = []
        for line in r.stdout.splitlines():
            try:
                path, _no, text = line.split(":", 2)
            except ValueError:
                continue
            if CANON_EXCLUDE.search(path):
                continue
            heading = text.lstrip().startswith("## ") and t.lower() in text.lower()
            hits.append((0 if heading else 1, path, _no, text))
        hits.sort(key=lambda x: x[0])
        for _rank, path, _no, text in hits[:2]:
            rel = os.path.relpath(path, os.path.dirname(WIKI_CACHE))
            lineno = None
            try:
                lineno = int(_no)
            except (TypeError, ValueError):
                lineno = None
            out.append({"path": path, "rel": rel, "lineno": lineno, "tag": t, "text": text})
    return out


def format_self_canon(addrs, focus=""):
    """주소 히트를 항목 사실문 기준으로 발췌한다."""
    out = []
    for a in addrs:
        excerpt = (section_body(a["path"], lineno=a["lineno"], needle=a["tag"], focus=focus)
                   or (a.get("text") or "").strip()[:400])
        if excerpt:
            out.append("%s :: [세션이 밝힌 주소 `%s`] %s" % (a["rel"], a["tag"], excerpt))
    return out


def self_canon_sections(digest, limit=6, focus=""):
    """세션이 밝힌 정본 주소의 **본문**을 캐시에서 직접 펴온다."""
    return format_self_canon(self_canon_addrs(digest, limit=limit), focus=focus)


def canon_snippets(query, limit=4, timeout=60):
    """Wiki 정본에서 관련 발췌를 가져온다. 실패하면 None (= 판정 불가)."""
    try:
        out = subprocess.run(["wiki-agent", "find", query], capture_output=True,
                             text=True, timeout=timeout).stdout
    except Exception:
        return None
    snips, cur = [], None
    for line in out.splitlines():
        m = re.match(r"\s*\d+\. score=[\d.]+ (\S+)", line)
        if m:
            cur = None if CANON_EXCLUDE.search(m.group(1)) else m.group(1)
            continue
        if cur and line.strip() and not line.strip().startswith(("load:", "[DOC-")):
            snips.append("%s :: %s" % (cur, line.strip()[:400]))
            cur = None
        if len(snips) >= limit:
            break
    return snips


def canon_dedup(items, model_cmd, timeout, digest=""):
    """정본에 이미 있는 항목을 걷어낸다 (2026-08-22 재측정 대응).

    실측: 판정 표본 47건에서 기각 31건 중 **23건(74%)** 이 '정본에 이미 있음'
    이었다. 가치 판정기는 세션 발췌만 보므로 이 축을 원리적으로 볼 수 없다 —
    프롬프트로는 못 고치고 검색 대조가 필요하다.

    앞서 시도한 값싼 대안('세션에 Wiki 기록 완료 신호가 있으면 억제')은 실측에서
    21/21 세션 전부 양성이라 변별력이 0이었다. 그래서 검색 대조로 간다.

    검색이 실패하면 **통과**시킨다. 여기서 fail-closed 하면 검색 장애가 전량
    폐기로 나타나고 감사로그엔 정상으로 찍힌다.
    """
    kept, dropped, total = [], [], {}
    self_addrs = self_canon_addrs(digest)
    # 리터럴 채널 인덱스 — 실행당 grep 한 번. 항목별 토큰 선정 규칙은 그대로
    # 두고 (같은 토큰이 같은 순서로 뽑힌다) 조회만 인메모리로 바꾼다.
    lit_index = build_literal_index(_collect_literal_tokens(items))
    for it in items:
        fact = it.get("fact", "") or ""
        q = (it.get("title", "") or "")[:60] + " " + fact[:120]
        snips = canon_snippets(q.strip())
        # 리터럴 채널 — 의미검색의 인덱스 지연과 표현 차이를 메운다. 두 채널을
        # 합쳐 넘기고 판정은 한 번만 한다(호출 수를 늘리지 않는다).
        lits = literal_hits(fact, index=lit_index)
        # 세션이 밝힌 주소의 정본 본문 — 검색이 못 찾는 구간을 정확히 메운다.
        # 발췌 창은 항목 사실문에 맞춘다(같은 LOG 라도 항목마다 다른 문단).
        self_addr = format_self_canon(self_addrs, focus=fact)
        lits = lits + [x for x in self_addr if x not in lits]
        if snips is None and not lits:
            it["_canon"] = "search_failed"
            kept.append(it)
            continue
        snips = (snips or []) + [x for x in lits if x not in (snips or [])]
        if not snips:
            kept.append(it)
            continue
        data, (err, usage, _r) = extract_json(
            CANON_PROMPT % (it.get("fact", ""), "\n".join("- " + x for x in snips)),
            model_cmd, timeout)
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + v
        if err or not isinstance(data, dict):
            kept.append(it)
            continue
        quote = str(data.get("quote", "") or "").strip()
        is_dup = str(data.get("duplicate", "")).lower().strip() == "yes"
        # 근거 문장을 못 대면 중복으로 치지 않는다. 판정기는 같은 입력에도 흔들리는데
        # (2026-08-22 실측: 동일 항목이 회차에 따라 keep↔dup 으로 뒤집힘), 인용을
        # 강제하면 근거 없는 yes 가 걸러진다.
        if is_dup and len(quote) < 10:
            # 근거 문장을 못 대는 중복 의심 — 버리지 않고 **표시해서 통과**시킨다.
            # 버리기/통과의 이분법을 쓸 이유가 없다. 이 파이프라인은 이미 3버킷이고,
            # 검토자에게 경로를 같이 주면 확인은 몇 초로 끝난다. 필터가 틀렸을 때의
            # 비용이 '지식 소실'에서 '표시 하나 틀림'으로 내려간다.
            # (실측 2026-08-22: 인용 강제로 부수피해는 0이 됐지만 재현율이 70%→48%로
            #  떨어졌다. 그 12건을 버리는 대신 표시하면 둘 다 잃지 않는다.)
            it["_canon_suspect"] = str(data.get("where", ""))[:120] or "(경로 미상)"
            kept.append(it)
            continue
        if is_dup:
            it["_canon_where"] = str(data.get("where", ""))[:120]
            it["_canon_quote"] = quote[:200]
            dropped.append((it, "canon_duplicate"))
        else:
            kept.append(it)
    return kept, dropped, total


def value_judge(items, model_cmd, timeout):
    """지속 가치 판정 (2026-08-22 손실률 80% 대응).

    함의검증은 **인용 정확도**만 재고 **지속 가치**는 재지 않는다. 그래서 한 줄을
    되풀이한 시시한 항목이 인용이 맞는다는 이유로 통과하고(실측 정밀도 50%),
    여러 줄을 종합한 핵심 발견이 인용을 넘었다는 이유로 격리됐다(손실률 80%).

    가치 판정을 **함의검증 앞에** 둔다. 가치 없는 항목을 먼저 걷어내면 비싼
    함의검증 호출을 아끼고, 남은 격리는 '가치 있는데 인용이 부실한 것'만 남는다.
    """
    kept, dropped, total = [], [], {}
    for it in items:
        data, (err, usage, _r) = extract_json(
            VALUE_PROMPT % (it.get("title", ""), it.get("fact", "")), model_cmd, timeout)
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + v
        if err or not isinstance(data, dict):
            kept.append(it)          # 판정 불가면 통과시켜 다음 단계가 보게 한다
            continue
        if str(data.get("valuable", "")).lower().strip() == "no":
            it["_value_why"] = str(data.get("why", ""))[:40]
            dropped.append((it, "no_durable_value"))
        else:
            kept.append(it)
    return kept, dropped, total


def repair_citation(it, digest, valid_ids, model_cmd, timeout):
    """함의 실패 항목의 인용을 보수한다.

    실패 대부분은 '주장이 거짓'이 아니라 '여러 줄을 종합해 인용 범위를 넘은 것'이다.
    그대로 격리하면 참인 지식이 인용 형식 문제로 묻힌다. 인용을 늘리거나 주장을
    인용 범위로 좁혀 승격 가능한 형태로 되돌린다.
    """
    quotes = "\n".join("- %s" % t for t in (it.get("_evidence_text") or []))
    data, (err, usage, _r) = extract_json(
        REPAIR_PROMPT % (it.get("fact", ""), quotes, digest), model_cmd, timeout)
    if err or not isinstance(data, dict):
        return None, usage
    fact = str(data.get("fact", "") or "").strip()
    ev = [e for e in (data.get("evidence") or [])
          if isinstance(e, str) and e[:8] in valid_ids]
    if not fact or not ev:
        return None, usage
    fixed = dict(it)
    fixed["fact"] = fact
    fixed["evidence"] = ev
    fixed["_evidence_text"] = [valid_ids.get(e[:8], "") for e in ev]
    fixed["_repaired"] = True
    return fixed, usage


def entail(items, model_cmd, timeout):
    """인용이 주장을 실제로 뒷받침하는지 검증한다 (V2 선결조건 #1).

    구조 검증(verify)은 "인용 id가 소스에 실재하는가"만 본다. 그것만으로는
    id만 맞으면 무관한 주장·환각·모순도 전부 통과한다 (2026-08-22 적대적 실측 3/3 통과).

    fail-closed: 판정을 받지 못하면 통과시키지 않는다. 다만 '판정 불가'와
    '판정 결과 불합격'은 다른 사건이므로 사유를 구분해 남긴다.

    항목을 한 번에 묶어 판정하면 **같은 항목의 판정이 배치에 뭐가 같이 들어있느냐에
    따라 뒤집힌다** (2026-08-22 실측: 동일 8건을 단독 배치로 재판정하니 2→5~6건 통과.
    반면 동일 배치 3회 반복은 8건 중 7건이 안정 → 원인은 비결정성이 아니라 배치 의존성).
    게이트는 각 주장을 그 주장의 인용만으로 판정해야 하므로 항목별로 호출한다.
    """
    if not items:
        return [], [], {}
    kept, dropped = [], []
    total = {}
    for it in items:
        quotes = "\n".join("- %s" % t for t in it.get("_evidence_text", []))
        data, (err, usage, _raw) = extract_json(
            ENTAIL_PROMPT % (it.get("fact", ""), quotes), model_cmd, timeout)
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + v
        if err or not isinstance(data, dict):
            dropped.append((it, "entail_unavailable"))   # fail-closed
            continue
        s = str(data.get("supported", "")).lower().strip()
        if s == "yes":
            kept.append(it)
        elif s == "no":
            it["_entail_why"] = str(data.get("why", ""))[:40]
            dropped.append((it, "not_entailed"))
        else:
            dropped.append((it, "entail_missing"))
    return kept, dropped, total


def verify(items, valid_ids):
    """증거 인용 강제 (구조). 인용 없거나 실재하지 않는 id면 폐기.

    주의: 이것은 id 실재 여부만 본다. 인용이 주장을 뒷받침하는지는 entail() 소관.
    """
    kept, dropped = [], []
    for it in items:
        ev = it.get("evidence") or []
        if not isinstance(ev, list) or not ev:
            dropped.append((it, "no_evidence"))
            continue
        good = [e for e in ev if isinstance(e, str) and e[:8] in valid_ids]
        if not good:
            dropped.append((it, "evidence_not_in_source"))
            continue
        if not (it.get("title") and it.get("fact")):
            dropped.append((it, "incomplete"))
            continue
        it["evidence"] = good
        it["_evidence_text"] = [valid_ids.get(e[:8], "") for e in good]
        kept.append(it)
    return kept, dropped


def main():  # noqa: C901 - orchestration kept aligned with deployed v6
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cap", type=int, default=CAP_PER_RUN)
    ap.add_argument("--timeout", type=int, default=300)
    # Explicit override remains available for controlled experiments. The
    # normal path resolves env -> bridge systemd env -> fleet paths -> PATH,
    # and a Piri node fails closed instead of silently invoking Claude (#1257).
    ap.add_argument("--model-cmd", default=None)
    ap.add_argument("--node", default=resolve_node())
    ap.add_argument("--out", default=os.path.join(HOME, ".hermes/logs/auto-distill-dryrun.jsonl"))
    ap.add_argument("--save-raw", action="store_true", help="dry-run 진단용: 모델 원출력 일부 저장")
    ap.add_argument("--only", default=None, help="이 문자열이 포함된 세션만 처리")
    ap.add_argument("--source", default="auto", choices=["auto", "piri", "claude"],
                    help="세션 소스. auto = 실재하는 소스 전부")
    ap.add_argument("--render-out", default=None,
                    help="AUTO.md 렌더 경로 (미지정 시 ~/.hermes/logs/auto-<node>.md)")
    ap.add_argument("--render-dry-run", action="store_true",
                    help="dry-run 이어도 AUTO.md 렌더는 수행 (형식 검증용, Wiki 미반영)")
    ap.add_argument("--quarantine",
                    default=os.path.join(HOME, ".hermes/logs/auto-distill-quarantine.jsonl"),
                    help="함의 미통과 항목 검토 레인")
    ap.add_argument("--no-cache-sync", action="store_true",
                    help="시작 시 Wiki 캐시 동기화 생략")
    ap.add_argument("--no-canon", action="store_true",
                    help="정본 중복 대조 생략 (대조 실험용)")
    ap.add_argument("--no-value", action="store_true",
                    help="가치 판정 생략 (대조 실험용)")
    ap.add_argument("--no-repair", action="store_true",
                    help="인용 보수 생략 (대조 실험용)")
    ap.add_argument("--no-entail", action="store_true",
                    help="함의 검증 생략 (진단 전용 — 구멍이 열린다)")
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="이보다 오래된 세션은 처리하지 않는다 (콜드스타트 폭주 방지). 0 = 무제한")
    args = ap.parse_args()

    try:
        selected_model = (
            resolve_explicit_model_command(args.model_cmd)
            if args.model_cmd is not None
            else resolve_model_command()
        )
    except ModelCommandError as exc:
        message = str(exc)
        print("FATAL: 모델 엔진 선택 실패 — %s" % message, file=sys.stderr)
        log_audit({"event": "engine_unavailable", "reason": message})
        return 2
    model_cmd = list(selected_model.argv)
    ENGINE_ENV.update(dict(getattr(selected_model, "env_overrides", ())))
    reason = " reason=%s" % selected_model.reason if selected_model.reason else ""
    print("모델 엔진: engine=%s source=%s%s"
          % (selected_model.engine, selected_model.source, reason))
    log_audit({
        "event": "engine_selected",
        "engine": selected_model.engine,
        "source": selected_model.source,
        "reason": selected_model.reason,
    })

    # INV-2: codex 로그는 어떤 경로로도 입력이 될 수 없다.
    fb = os.path.realpath(FORBIDDEN)
    # 리터럴 정본 대조는 캐시 신선도에 달려 있다. 임베딩 인덱스와 달리 rsync 는
    # 싸므로(실측 ~4s) 매 회차 앞에서 당긴다. 실패해도 계속 간다 — 여기서 멈추면
    # 동기화 장애가 추출 정지로 번진다.
    if not args.no_cache_sync:
        try:
            subprocess.run(["wiki-agent", "sync-cache"], capture_output=True,
                           text=True, timeout=180)
        except Exception as e:
            print("      ⚠ Wiki 캐시 동기화 실패 (리터럴 대조가 낡을 수 있음): %s" % e)

    # INV-2: codex 로그는 어떤 경로로도 입력이 될 수 없다.
    # 예외 (#1295): CCC_AUTO_DISTILL_CODEX=1 인 노드(codex 레인)는 추출 엔진도
    # codex 로 리졸브됐을 때만 원본 codex 롤아웃을 소스로 허용한다. 추출 호출은
    # CODEX_HOME 스크래치 리다이렉트로 격리되므로(model_command 참조) 추출기의
    # 세션이 원본 트리에 떨어지는 일이 구조적으로 없다 — INV-2 ④ 실측이 이를
    # 입증한다. 다른 엔진이면 격리가 없으므로 fail-closed.
    codex_opt_in = os.environ.get("CCC_AUTO_DISTILL_CODEX", "").strip() == "1"
    sources = dict(SOURCES)
    if codex_opt_in:
        sources["codex"] = FORBIDDEN
    picked = [(k, v) for k, v in sources.items()
              if (args.source in ("auto", k)) and os.path.isdir(v)]
    if codex_opt_in and picked and any(k == "codex" for k, _ in picked) \
            and getattr(selected_model, "engine", None) != "codex":
        print("FATAL: codex 소스는 codex 엔진(CODEX_HOME 격리)에서만 읽을 수 있다",
              file=sys.stderr)
        return 2
    for name, d in picked:
        rd = os.path.realpath(d)
        if rd == fb or rd.startswith(fb + os.sep):
            if codex_opt_in and name == "codex":
                continue  # 위 가드가 통과된 경우만 여기 온다
            print("FATAL: 소스 %s 가 codex 로그를 가리킨다" % name, file=sys.stderr)
            return 2
    if not picked:
        print("FATAL: 이 노드에 사용 가능한 세션 소스가 없다", file=sys.stderr)
        return 2

    # 단일 실행 보장. TM-2380 설계 §5 가 flock 을 요구하는데 구현이 빠져 있었다.
    # 크론 주기(30분)보다 실행이 길어지면 두 회차가 겹치고, 워터마크가 아직
    # 갱신되지 않은 상태라 **같은 세션을 두 번 추출**해 비용이 이중으로 나간다.
    # 겹침은 조용히 일어나므로(둘 다 "성공"으로 찍힌다) 락이 유일한 방어다.
    lock_path = os.path.join(HOME, ".hermes/state/auto-distill.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        import fcntl
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        print("이미 실행 중이다 — 이번 회차를 건너뛴다 (중복 추출 방지)", file=sys.stderr)
        log_audit({"event": "skip_run", "reason": "locked"})
        return 0

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    wm = load_watermark()
    import glob
    files = []
    for name, d in picked:
        # codex 롤아웃은 세대/월/일 3단 아래에 있다 (sessions/YYYY/MM/DD/*.jsonl).
        got = (glob.glob(d + "/*/*.jsonl") + glob.glob(d + "/*.jsonl")
               + glob.glob(d + "/*/*/*/*.jsonl"))
        print("소스 %s: %d개" % (name, len(got)))
        files += got
    # stat 은 파일당 한 번만 한다 — 정렬키·지평·워터마크(·워터마크 저장)가 같은
    # 값을 재사용한다. 나열과 stat 사이에 사라진 파일은 조용히 제외한다
    # (예전에는 정렬 도중 getmtime 이 예외를 던져 회차 전체가 죽었다).
    mtimes = {}
    for f in set(files):
        try:
            mtimes[f] = os.path.getmtime(f)
        except OSError:
            continue
    files = sorted(mtimes, key=mtimes.__getitem__, reverse=True)

    # 콜드스타트 지평. 워터마크가 없는 첫 회차에는 모든 과거 세션이 "신규"로 보인다
    # (실측 2026-08-22: 소교 697 · 노숙 193 · 곽가 90 · 육손 78). 지평이 없으면
    # 수개월 전 세션에서 이미 낡았거나 이미 기록된 사실을 다시 뽑느라 예산을 태운다.
    todo = []
    self_calls = 0
    too_old = 0
    horizon = time.time() - args.max_age_days * 86400 if args.max_age_days > 0 else 0
    for f in files:
        mtime = mtimes[f]
        if horizon and mtime < horizon:
            too_old += 1
            continue
        # 싼 게이트(워터마크/mtime)를 먼저 — 변화 없는 파일은 열지 않는다.
        # 예전에는 is_self_call 이 먼저라 1000+ 파일 전부를 매 회차 열어 JSON
        # 파싱했다. 워터마크로 걸러진 자기호출 세션은 이제 skip(self_call)
        # 감사 이벤트를 매 회차 남기지 않는다 — 의도된 로그 소음 제거.
        key = hashlib.sha256(f.encode()).hexdigest()[:16]
        prev = wm.get(key, {})
        if not (prev.get("lines", 0) == 0 or mtime > prev.get("mtime", 0)):
            continue
        if is_self_call(f):
            self_calls += 1
            log_audit({"event": "skip", "session": os.path.basename(f)[:24],
                       "reason": "self_call"})
            continue
        todo.append((f, key, prev.get("lines", 0)))
    if args.only:
        todo = [t for t in todo if args.only in os.path.basename(t[0])]
    skipped = max(0, len(todo) - args.cap)
    todo = todo[:args.cap]
    if too_old:
        print("지평 밖 세션 %d개 제외 (>%d일)" % (too_old, args.max_age_days))
        log_audit({"event": "horizon", "skipped": too_old, "max_age_days": args.max_age_days})
    if self_calls:
        print("자기호출 세션 %d개 배제 (되먹임 차단)" % self_calls)
    if skipped:
        print("CAP: %d개 세션을 이번 회차에서 제외 (다음 회차 처리)" % skipped)

    render_path = args.render_out or os.path.join(HOME, ".hermes/logs/auto-%s.md" % args.node)
    rendered_add = rendered_dup = 0
    results = []
    for f, key, since in todo:
        t0 = time.time()
        digest, ids, total = digest_session(f, since)
        name = os.path.basename(f)[:24]
        if len(digest) < MIN_TEXT:
            # INV-2: '스키마를 한 줄도 인식 못 함'과 '내용이 적음'은 다른 사건이다.
            # 전자를 too_small 로 뭉뚱그리면 어댑터 결함이 감사로그에서 정상으로 보인다.
            unknown = not getattr(iter_messages, "last_schema_ok", True)
            reason = "unknown_schema" if unknown else "too_small"
            print("  skip  %s  (%s, 증가분 %dB < %dB)" % (name, reason, len(digest), MIN_TEXT))
            log_audit({"event": "skip", "session": name, "reason": reason, "bytes": len(digest)})
            if not args.dry_run:
                # 스캔 시점 mtime 을 기록한다: 다이제스트 이후 붙은 내용이 있으면
                # 파일 mtime 이 이보다 새로워져 다음 회차가 반드시 다시 본다.
                wm[key] = {"lines": total, "mtime": mtimes[f], "path": f}
            continue
        print("  추출  %s  증가분 %dB / %d줄 ... " % (name, len(digest), total - since), end="", flush=True)
        data, (err, usage, raw) = extract(digest, model_cmd, args.timeout)
        dt = time.time() - t0
        if err:
            print("실패 (%s, %.1fs)" % (err, dt))
            log_audit({"event": "extract_fail", "session": name, "error": err,
                       "sec": round(dt, 1), "usage": usage})
            continue
        items = data.get("items", []) if isinstance(data, dict) else []
        kept, dropped = verify(items, ids)
        n_struct = len(kept)
        # V2 선결조건 #1: 구조 검증(id 실재)만으로는 무관·환각·모순이 통과한다.
        # 통과분을 다시 함의 검증에 건다. fail-closed.
        #
        # 2026-08-22 첫 판정 실측(정밀도 50% / 손실률 80%)에 따라 두 단계를 더 둔다:
        #   가치 판정 — 함의 **앞**에. 시시한 항목을 먼저 걷어내 정밀도를 올리고
        #               비싼 함의 호출을 아낀다.
        #   인용 보수 — 함의 **뒤**에. 실패 대부분은 '거짓'이 아니라 '여러 줄을
        #               종합해 인용을 넘은 것'이라, 격리 전에 되살릴 기회를 준다.
        eu = {}
        quarantined = []
        n_value = n_struct
        n_canon = n_struct
        n_repaired = 0
        if kept and not args.no_entail:
            if not args.no_value:
                kept, vdrop, vu = value_judge(kept, model_cmd, args.timeout)
                dropped += vdrop
                n_value = len(kept)
                for k, v in (vu or {}).items():
                    if isinstance(v, (int, float)):
                        eu[k] = eu.get(k, 0) + v
            # 정본 중복 — 가치 판정 **뒤**, 함의 **앞**. 뒤에 두는 이유: 가치
            # 없는 항목까지 검색을 돌리면 질의만 낭비된다. 앞에 두는 이유:
            # 중복은 인용이 맞아도 올리면 안 되므로 비싼 함의 호출을 아낀다.
            if not args.no_canon:
                kept, cdrop, cu = canon_dedup(kept, model_cmd, args.timeout, digest)
                dropped += cdrop
                n_canon = len(kept)
                for k, v in (cu or {}).items():
                    if isinstance(v, (int, float)):
                        eu[k] = eu.get(k, 0) + v
            kept, quarantined, eu2 = entail(kept, model_cmd, args.timeout)
            for k, v in (eu2 or {}).items():
                if isinstance(v, (int, float)):
                    eu[k] = eu.get(k, 0) + v
            # 인용 보수 — 함의 실패분을 되살려 본다. 실패하면 그대로 격리.
            if quarantined and not args.no_repair:
                still = []
                for it, why in quarantined:
                    if why != "not_entailed":
                        still.append((it, why))
                        continue
                    fixed, ru = repair_citation(it, digest, ids, model_cmd, args.timeout)
                    for k, v in (ru or {}).items():
                        if isinstance(v, (int, float)):
                            eu[k] = eu.get(k, 0) + v
                    if not fixed:
                        still.append((it, why))
                        continue
                    ok, bad, ru2 = entail([fixed], model_cmd, args.timeout)
                    for k, v in (ru2 or {}).items():
                        if isinstance(v, (int, float)):
                            eu[k] = eu.get(k, 0) + v
                    if ok:
                        kept.append(ok[0])
                        n_repaired += 1
                    else:
                        still.append((it, "not_entailed_after_repair"))
                quarantined = still
        dt = time.time() - t0
        print("후보 %d → 구조 %d → 가치 %d → 정본 %d → 함의+보수 %d(보수 %d), 격리 %d, 폐기 %d (%.1fs)"
              % (len(items), n_struct, n_value, n_canon, len(kept), n_repaired,
                 len(quarantined), len(dropped), dt))
        # 구조 미달(인용 자체가 없거나 실재하지 않음)은 검토할 것이 없으므로 폐기.
        for it, why in dropped:
            log_audit({"event": "drop", "session": name, "reason": why, "title": it.get("title", "")[:80]})
        # 함의 미통과는 '유효한 인용은 있으나 뒷받침이 의심되는' 상태다. 폐기하지 않고
        # 검토 레인으로 보낸다 — 판정기의 경계 불안정(실측 25%)을 사람이 흡수한다.
        for it, why in quarantined:
            write_quarantine(args.quarantine, {
                "node": args.node, "session": name, "path": f, "reason": why,
                "why": it.get("_entail_why", ""), "kind": it.get("kind", ""),
                "title": it.get("title", ""), "fact": it.get("fact", ""),
                "evidence": it.get("evidence", []),
                "evidence_text": it.get("_evidence_text", []),
            })
            log_audit({"event": "quarantine", "session": name, "reason": why,
                       "title": it.get("title", "")[:80]})
        # 함의검증 호출분을 합산하지 않으면 비용이 과소보고된다.
        for k, v in (eu or {}).items():
            if isinstance(v, (int, float)) and isinstance(usage.get(k), (int, float)):
                usage[k] = usage[k] + v
        rec = {"session": name, "path": f, "kept": kept,
               "dropped_detail": [{"title": d[0].get("title", ""), "why": d[1],
                                   "claimed_evidence": d[0].get("evidence")} for d in dropped],
               "quarantined_detail": [{"title": q[0].get("title", ""), "why": q[1],
                                       "entail_why": q[0].get("_entail_why", ""),
                                       "claimed_evidence": q[0].get("evidence")} for q in quarantined],
               "quarantined": len(quarantined),
               "dropped": len(dropped), "sec": round(dt, 1), "usage": usage,
               "available_ids": sorted(ids.keys()), "raw_head": raw[:1500] if args.save_raw else None}
        results.append(rec)
        with open(args.out, "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not usage:
            # 계측 실패는 조용히 0으로 보이면 안 된다 — V2 심의 기준이 비용이다.
            print("      ⚠ 사용량 미계측 (model-cmd 가 usage 를 내보내지 않음)")
            log_audit({"event": "usage_missing", "session": name})
        # 파이프라인 버전. 이게 없으면 관측이 **다른 파이프라인의 실행을 섞어 잰다**
        # (실측 2026-08-22: 함의검증 도입 전 실행이 섞여 통과율이 83%로 부풀고,
        # 봉투 수정 전 비용 0 실행이 평균 비용을 희석했다).
        #   1 = 구조검증만  2 = +함의검증  3 = +격리레인/비용계측
        #   4 = +가치판정/인용보수 (정밀도·손실률 대응)
        log_audit({"event": "extract", "pipeline": PIPELINE, "session": name,
                   "candidates": len(items),
                   "value_kept": n_value, "canon_kept": n_canon, "repaired": n_repaired,
                   "kept": len(kept), "quarantined": len(quarantined),
                   "dropped": len(dropped), "sec": round(dt, 1),
                   "usage": usage, "dry_run": args.dry_run})
        # AUTO.md 렌더. dry-run 계약상 기본은 미작성이며, 형식 검증이 필요할 때만
        # --render-dry-run 으로 연다 (그래도 Wiki 에는 반영되지 않는다).
        if (not args.dry_run) or args.render_dry_run:
            chunk = render_items(args.node, kept, quarantined, name,
                                 time.strftime("%Y-%m-%d %H:%M %Z"))
            if chunk:
                added, skipped_dup = merge_auto_md(render_path, args.node, chunk)
                rendered_add += added
                rendered_dup += skipped_dup

        if not args.dry_run:
            wm[key] = {"lines": total, "mtime": mtimes[f], "path": f}

    print()
    print("=" * 60)
    tot_kept = sum(len(r["kept"]) for r in results)
    tot_drop = sum(r["dropped"] for r in results)
    tot_quar = sum(r.get("quarantined", 0) for r in results)
    print("세션 %d개 처리 · 승격후보 %d건 · 검토격리 %d건 · 증거미달 폐기 %d건"
          % (len(results), tot_kept, tot_quar, tot_drop))
    if tot_quar:
        print("격리 레인: %s (검토 대기 — 폐기 아님)" % args.quarantine)
    if results:
        print("소요 합계 %.1fs (평균 %.1fs/세션)" % (
            sum(r["sec"] for r in results), sum(r["sec"] for r in results) / len(results)))
        ti = sum((r.get("usage") or {}).get("inputTokens", 0) for r in results)
        to = sum((r.get("usage") or {}).get("outputTokens", 0) for r in results)
        tc = sum((r.get("usage") or {}).get("cacheReadTokens", 0) for r in results)
        cost = sum((r.get("usage") or {}).get("costUsd", 0) or 0 for r in results)
        print("토큰 input=%d output=%d cacheRead=%d (평균 input %d/세션)"
              % (ti, to, tc, ti // len(results)))
        if cost:
            print("비용 $%.4f (평균 $%.4f/세션) — V2 램프 심의 입력" % (cost, cost / len(results)))
        else:
            print("비용 계측 없음 — 모델 호출이 사용량을 내보내지 않았다 (claude 는 --output-format json 필요)")
    print("=" * 60)
    for r in results:
        if not r["kept"]:
            continue
        print("\n## %s" % r["session"])
        for it in r["kept"]:
            print("  - [%s] %s" % (it.get("kind", "?"), it.get("title")))
            print("      %s" % it.get("fact"))
            print("      근거id: %s" % ", ".join(it["evidence"]))
            for et in it.get("_evidence_text", []):
                print("        └ %s" % et[:160])

    if tot_quar:
        print("\n" + "-" * 60)
        print("검토 격리 (%d건) — 인용은 유효하나 뒷받침이 의심됨. 사람이 판단할 것" % tot_quar)
        for r in results:
            for q in r.get("quarantined_detail", []):
                print("  - [%s] %s" % (q["why"] or q["reason"], q["title"][:70]))

    if rendered_add or rendered_dup:
        print("\nAUTO.md: %s" % render_path)
        print("  신규 %d건 기록 · 중복 %d건 스킵(멱등)" % (rendered_add, rendered_dup))

    if args.dry_run:
        if args.render_dry_run:
            print("\n[DRY-RUN] 워터마크 미갱신 · PR 미생성 · AUTO.md 는 로컬 렌더만(Wiki 미반영)")
        else:
            print("\n[DRY-RUN] 워터마크 미갱신 · PR 미생성 · AUTO.md 미작성")
    if not args.dry_run:
        save_watermark(wm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
