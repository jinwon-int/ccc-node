#!/usr/bin/env python3
"""TM-2380 카나리 관측 — V2 심의 입력 산출.

Managed with auto-distill.py by ``scripts/install-auto-distill.sh``.

왜 통과율만으로는 안 되는가 (2026-08-22 실측):
  함의검증은 **인용 정확도**를 재고 **지속 가치**는 재지 않으며, 실무에서 역상관한다.
  첫 게시 9건에서 승격 4건 중 2건이 무가치였고, 세션의 핵심 발견 5건은 전부 격리
  쪽에 있었다. 따라서 "통과율 N%"는 V2 심의의 근거가 되지 못한다.

  V2가 실제로 답해야 하는 질문은 두 개다:
    (1) 게이트가 통과시킨 것 중 **정말 올릴 만한 것**이 얼마나 되나 (정밀도)
    (2) 게이트가 막은 것 중 **올렸어야 할 것**이 얼마나 되나 (손실률)

  (2)는 사람 판정 없이는 측정 불가능하다. 그래서 이 스크립트는 파이프라인 로그와
  **사람이 AUTO.md에 남긴 판정**을 함께 읽는다. 판정이 없으면 그 사실을 드러낸다 —
  판정 없는 통과율을 V2 근거로 쓰는 것이 정확히 이 도구가 막으려는 실수다.

판정 남기는 법 (사람):
  AUTO.md 항목의 `**상태**: unverified` 를 아래 중 하나로 바꾼다.
    promoted      정본으로 승격했다 (= 올릴 만했다)
    discarded     버렸다 (= 올릴 만하지 않았다)
    fix-citation  내용은 맞으나 인용이 부실하다 (= 올릴 만한데 게이트가 옳게 막았다)
  본문은 지워도 되지만 `**키**:` 줄은 남긴다.
"""
import argparse
from collections import Counter, defaultdict
import json
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
AUDIT = os.path.join(HOME, ".hermes/logs/auto-distill-audit.jsonl")
WIKI_CACHE = os.path.join(HOME, ".wiki-agent/wiki-cache/pages/nodes")
NODES = ["gwakga", "nosuk", "yukson", "sogyo", "daegyo"]

def _current_pipeline(default=5):
    """현행 파이프라인 버전을 **추출기에서 직접 읽는다.**

    두 파일에 숫자를 따로 적어두면 반드시 어긋난다 — 실측 2026-08-22: 추출기를
    v5로 올렸는데 여기가 4로 남아, v5 실행이 전부 '구버전'으로 제외되고 화면엔
    "구버전 N건 제외"만 떴다. 카운터가 영원히 0인데 도구는 정상으로 보인다.
    소스를 하나로 만들어 드리프트 자체를 없앤다.
    """
    path = os.path.join(HOME, ".hermes/auto-distill/auto-distill.py")
    try:
        for line in open(path, errors="replace"):
            m = re.match(r"\s*PIPELINE\s*=\s*(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return default


CURRENT = _current_pipeline()

# 사람 판정 → 그 항목이 '올릴 만했는가'
VALUABLE = {"promoted": True, "fix-citation": True, "discarded": False}


def read_audit(node, local_node):
    """노드의 감사로그를 읽는다. 원격은 ssh."""
    if node == local_node:
        if not os.path.exists(AUDIT):
            return []
        raw = open(AUDIT, errors="replace").read()
    else:
        try:
            raw = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", node,
                 "cat ~/.hermes/logs/auto-distill-audit.jsonl 2>/dev/null || true"],
                capture_output=True, text=True, timeout=90).stdout
        except Exception:
            return None          # 도달 실패는 '0건'과 구별한다
    out = []
    for line in raw.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


ITEM_RE = re.compile(
    r"^### (?P<mark>[✅🔍])[^\n]*\n(?P<body>.*?)(?=\n### |\Z)", re.S | re.M)
STATUS_RE = re.compile(r"\*\*상태\*\*:\s*`?([a-z-]+)`?")
KEY_RE = re.compile(r"\*\*키\*\*:\s*`([0-9a-f]{12})`")
# 항목이 어느 파이프라인 구성에서 나왔는지. 각인이 없으면 도입 이전(v<=3) 항목이다.
PIPE_RE = re.compile(r"\*\*파이프라인\*\*:\s*`v(\d+)`")


def read_verdicts(node):
    """AUTO.md 에서 (원래 버킷, 사람 판정) 을 수집한다."""
    p = os.path.join(WIKI_CACHE, node, "AUTO.md")
    if not os.path.exists(p):
        return []
    txt = open(p, errors="replace").read()
    rows = []
    for m in ITEM_RE.finditer(txt):
        body = m.group("body")
        st = STATUS_RE.search(body)
        k = KEY_RE.search(body)
        pv = PIPE_RE.search(body)
        rows.append({
            "node": node,
            "bucket": "pass" if m.group("mark") == "✅" else "quarantine",
            "status": (st.group(1) if st else "?"),
            "key": (k.group(1) if k else "?"),
            "pipeline": (int(pv.group(1)) if pv else None),
        })
    return rows


def main():  # noqa: C901 - report assembly mirrors the deployed collector
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=os.environ.get(
        "CCC_AUTO_NODE") or {"vps7": "gwakga", "vps5": "yukson",
        "racknerd-167be94": "sogyo", "localhost": "daegyo"}.get(
        os.uname().nodename, os.uname().nodename),
        help="이 노드의 위키 노드명 (호스트명이 아니다 — auto-distill.NODE_ALIASES 와 동일)")
    ap.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    args = ap.parse_args()

    runs = defaultdict(list)
    unreachable = []
    for n in NODES:
        rs = read_audit(n, args.node)
        if rs is None:
            unreachable.append(n)
            continue
        runs[n] = [r for r in rs if r.get("event") == "extract"]

    # 파이프라인 버전이 다른 실행을 섞으면 지표가 조용히 거짓이 된다.
    # 실측 2026-08-22: 함의검증 도입 전 실행(구조검증만 = kept)이 섞여 통과율이
    # 83%로 부풀었고, 사용량 봉투 수정 전 실행(비용 0)이 평균 비용을 희석했다.
    # 현행 버전만 집계하고, 제외분은 **조용히 버리지 않고 보고**한다.
    legacy = 0
    for n in list(runs):
        cur = []
        for r in runs[n]:
            v = r.get("pipeline")
            if v is None:
                # 버전 필드 도입 이전 레코드. quarantined 필드 유무로 추정한다.
                v = 2 if "quarantined" in r else 1
            if v == CURRENT:
                cur.append(r)
            else:
                legacy += 1
        runs[n] = cur

    # ── 파이프라인 지표 ──────────────────────────────────────────────
    tot = Counter()
    cost = 0.0
    per_session_pass = []
    for n, rs in runs.items():
        for r in rs:
            c = r.get("candidates", 0) or 0
            k = r.get("kept", 0) or 0
            q = r.get("quarantined", 0) or 0
            d = r.get("dropped", 0) or 0
            tot["sessions"] += 1
            tot["candidates"] += c
            tot["kept"] += k
            tot["quarantined"] += q
            tot["dropped"] += d
            tot["sec"] += r.get("sec", 0) or 0
            cost += (r.get("usage") or {}).get("costUsd", 0) or 0
            if c:
                per_session_pass.append(k / c)

    # ── 사람 판정 ────────────────────────────────────────────────────
    verdicts = []
    for n in NODES:
        verdicts += read_verdicts(n)
    judged_all = [v for v in verdicts if v["status"] in VALUABLE]
    unjudged = [v for v in verdicts if v["status"] not in VALUABLE]
    # 로그만 버전으로 거르고 사람 판정을 섞으면, 로그 쪽에서 막은 혼합 오염이
    # 판정 쪽으로 그대로 되돌아온다. 정밀도/손실률은 **현행 구성 항목만**으로 낸다.
    judged = [v for v in judged_all if v.get("pipeline") == CURRENT]
    judged_legacy = [v for v in judged_all if v.get("pipeline") != CURRENT]

    def rate(rows, bucket):
        sel = [r for r in rows if r["bucket"] == bucket]
        if not sel:
            return None, 0
        good = sum(1 for r in sel if VALUABLE[r["status"]])
        return good / len(sel), len(sel)

    precision, n_pass = rate(judged, "pass")        # 통과분 중 올릴 만했던 비율
    loss, n_quar = rate(judged, "quarantine")       # 격리분 중 올렸어야 했던 비율

    if args.json:
        print(json.dumps({
            "pipeline": dict(tot), "costUsd": round(cost, 4),
            "unreachable": unreachable,
            "judged": len(judged), "unjudged": len(unjudged),
            "judged_legacy": len(judged_legacy), "verdict_pipeline": CURRENT,
            "gate_precision": precision, "gate_loss": loss,
        }, ensure_ascii=False))
        return 0

    print("=" * 62)
    print("TM-2380 카나리 관측")
    print("=" * 62)
    if unreachable:
        print("⚠ 도달 실패 노드: %s — 아래 수치는 이 노드들을 뺀 값이다" % ", ".join(unreachable))
    if legacy:
        print("구버전 파이프라인 실행 %d건 제외 (현행 v%d 만 집계)" % (legacy, CURRENT))
    if not tot["sessions"]:
        # 파이프라인 실행이 없어도 사람 판정은 계속 본다 — 이미 게시된 항목의
        # 판정은 파이프라인 재가동과 무관하게 V2 심의의 근거이기 때문이다.
        print("현행 파이프라인 실행 기록 없음 — 아래 판정 지표만 유효하다.")
    print("세션 %d · 후보 %d · 통과 %d · 격리 %d · 폐기 %d"
          % (tot["sessions"], tot["candidates"], tot["kept"], tot["quarantined"], tot["dropped"]))
    if tot["sessions"]:
        print("소요 %.0fs (평균 %.0fs/세션) · 비용 $%.4f (평균 $%.4f/세션)"
              % (tot["sec"], tot["sec"] / tot["sessions"], cost, cost / tot["sessions"]))
    if per_session_pass:
        lo, hi = min(per_session_pass), max(per_session_pass)
        avg = sum(per_session_pass) / len(per_session_pass)
        print("세션별 통과율: 평균 %.0f%% · 범위 %.0f~%.0f%% (n=%d)"
              % (avg * 100, lo * 100, hi * 100, len(per_session_pass)))
        if hi - lo > 0.4:
            print("  ⚠ 편차가 크다 — 단일 회차로 결론짓지 말 것")

    print()
    print("─ 사람 판정 (V2 심의의 실제 근거) " + "─" * 26)
    print("판정 완료 %d건 (현행 v%d) · 미판정 %d건" % (len(judged), CURRENT, len(unjudged)))
    if judged_legacy:
        print("  구버전 구성 항목 %d건은 지표에서 제외 — 참고용으로만 본다" % len(judged_legacy))
    if not judged:
        print()
        print("  ⚠ 판정이 0건이다. **통과율만으로 V2를 심의할 수 없다** —")
        print("    함의검증은 인용 정확도를 재고 지속 가치는 재지 않으며 역상관한다.")
        print("    AUTO.md 항목의 `**상태**` 를 promoted/discarded/fix-citation 으로 바꿔라.")
        if judged_legacy:
            print("    (구버전 판정 %d건이 있으나 구성이 달라 현행 심의 근거가 아니다)"
                  % len(judged_legacy))
        return 0

    # 선택 편향 경고: 검토자가 '승격할 것'만 골라 판정하면 손실률이 100% 로 부푼다.
    # 분모를 항상 드러내고, 판정 커버리지가 낮으면 지표를 믿지 말라고 말한다.
    tot_pass = sum(1 for v in verdicts if v["bucket"] == "pass")
    tot_quar = sum(1 for v in verdicts if v["bucket"] == "quarantine")
    if precision is not None:
        print("게이트 정밀도 : %.0f%%  (통과분 판정 %d/%d건)"
              % (precision * 100, n_pass, tot_pass))
    if loss is not None:
        print("게이트 손실률 : %.0f%%  (격리분 판정 %d/%d건)"
              % (loss * 100, n_quar, tot_quar))
    cov = []
    if tot_pass and n_pass / tot_pass < 0.8:
        cov.append("통과분 %d/%d" % (n_pass, tot_pass))
    if tot_quar and n_quar / tot_quar < 0.8:
        cov.append("격리분 %d/%d" % (n_quar, tot_quar))
    if cov:
        print("  ⚠ 판정 커버리지가 낮다 (%s). 검토자가 일부만 판정하면" % ", ".join(cov))
        print("    선택 편향으로 위 비율이 실제와 크게 달라진다 — 전건 판정 후 볼 것.")
        if loss > 0.5:
            print("  ⚠ 게이트가 가치 있는 항목을 절반 넘게 막고 있다 —")
            print("    승격 판정에 가치 축을 넣기 전에는 V2 자동 승격을 켜면 안 된다.")
    promoted = sum(1 for v in judged if v["status"] == "promoted")
    if promoted and cost:
        print("승격 1건당 비용: $%.4f" % (cost / promoted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
