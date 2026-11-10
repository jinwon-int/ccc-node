#!/usr/bin/env python3
"""Prompt-time skill routing over the installed skill roots (ccc-node #2645).

Why this exists
---------------
Claude Code picks a skill from the per-turn listing, and that listing is
capped at ~1 % of the context window. ``ccc-skill-listing-policy`` keeps the
description only for the core set and recently used skills; on a node with
~270 skills everything else is a bare name. Routing then degrades to name
tokens, and a skill nobody has used stays invisible (self-reinforcing). The
``family-skills`` MCP search exists but is pull-only: the agent must decide to
search before it knows a skill applies.

This module is the push side. ``suggest`` runs as a ``UserPromptSubmit`` hook:
it scores the submitted prompt against every installed skill's ``name`` +
``description`` (BM25, stdlib) and injects the top candidates — name plus a
trimmed description — as ``additionalContext``. Nothing is executed or
auto-invoked; the model still chooses. Core-listing skills are excluded
because their descriptions are already in the listing.

A suggestion that does not fit is worse than none (#2748: at the first
thresholds 0 of 6 suggestions on 14 days of real prompts fitted), so a
candidate has to earn its line:

* evidence is counted per prompt *concept* — one word the operator wrote,
  however many English tokens the synonym map gives it;
* two distinctive concepts must be shared and hit the skill's name — or
  four, in a prompt that is mostly about them (tokens common to a whole
  skill family are not evidence);
* the shared concepts must cover ``MIN_COVERAGE`` of the prompt's
  ``COVERAGE_WINDOW`` heaviest concepts, so a long prompt is not out of reach;
* pure questions need one more concept; probes, pasted documents,
  approvals and option echoes are skipped.

The index spans every runtime's skill root, but ``suggest`` only offers the
skills the *calling* runtime can invoke (#2658): a Codex-only name suggested
to Claude is a name the Skill tool cannot resolve. The hook is registered in
Claude Code's hooks only, so the runtime defaults to ``claude``;
``CCC_SKILL_ROUTER_RUNTIME`` selects another root, and an unknown value makes
the hook silent rather than wrong.

Two more commands reuse the same index:

* ``check-description`` — a draft's description must retrieve the draft ahead
  of every installed skill (self-retrieval) and carry enough distinct content
  tokens. ``autoinstall.sh gate_lint`` uses it as ``description-not-routable``
  so an unroutable description never installs unattended.
* ``report`` — joins the body-free suggestion ledger with the skill-usage
  ledger: how often a suggestion was followed, what was invoked without one,
  what keeps being suggested and never used. The numbers that decide the
  later tiering/consolidation layers.

Contract (matches the other hook libs): stdlib only, fail-open on every
malformed input, body-free ledgers (never the prompt text), owner-only files.

Usage::

    skill_router.py suggest                 # hook: reads the hook JSON on stdin
    skill_router.py query "<text>" [--limit N] [--json]
    skill_router.py check-description --name N (--description D | --stdin) [--json]
    skill_router.py report [--days N] [--json]
    skill_router.py index [--rebuild] [--json]
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --- tunables ----------------------------------------------------------------------

#: Candidates shown per prompt. Three is enough to be useful and small enough
#: not to crowd the turn.
MAX_CANDIDATES = 3
#: Sanity floor on the BM25 score (absolute scores scale with corpus size —
#: IDF is ~3× larger on 280 skills than on 7 — so the real acceptance tests
#: are the concept and coverage rules below).
MIN_SCORE = 2.0
#: Fraction of the prompt's IDF mass a candidate must cover: how much of what
#: the operator said is actually about this skill. 0.4 keeps "restart the
#: bridge detached" → the restart skill and drops a prompt that merely shares
#: two words with something. The mass is measured over prompt *concepts* and
#: capped by ``COVERAGE_WINDOW``.
MIN_COVERAGE = 0.4
#: Coverage is judged against the prompt's this-many heaviest concepts only.
#: Without the cap coverage falls with prompt length: on 14 days of real
#: prompts (#2748) 121 of 192 had a best candidate under 0.1 and long
#: prompts could not reach ``MIN_COVERAGE`` at all. Six is the 90th
#: percentile of concepts in a typed prompt (300 characters or less) on that
#: set, so an ordinary request is scored as before and a longer one needs
#: about as much shared evidence as a six-word one (three to five
#: distinctive concepts on a ~320-skill index).
COVERAGE_WINDOW = 6
#: A candidate must share this many distinctive prompt *concepts*. A concept
#: is one thing the operator wrote: an ASCII word, or one synonym surface
#: with all of its English targets. Counting tokens instead let a single word
#: pass alone ("테스트" → test + tests, "a2a" → four tokens): every suggestion
#: made at the old thresholds on the #2748 set came from that, and none
#: fitted. Distinctive means not generic (below): two family words ("issue" +
#: "merge") name a topic, not a skill.
MIN_SHARED_CONCEPTS = 2
#: … and this many of them must hit the skill's *name* — one when the name
#: has a single distinctive token; a name made of family words alone cannot
#: be matched. The name is the skill's own summary: a prompt that shares only
#: description vocabulary (status, failed, result, file …) is about something
#: else. On the #2748 set every fitting suggestion named two such words
#: (merge queue, legal precedent, deploy + web) and no wrong one on a short
#: prompt did.
MIN_NAME_CONCEPTS = 2
#: Without the name, this many shared distinctive concepts are needed and
#: ``MIN_COVERAGE`` must hold over the whole prompt, not the window: an
#: operator describing the symptom in the description's own words ("ufw
#: allow rules after the cve advisory") is still routed, a pasted report
#: that happens to contain a few of them is not. Twice the name requirement;
#: no prompt in the #2748 set matched this way, right or wrong.
MIN_DESCRIPTION_CONCEPTS = 4
#: A token found in more than this fraction of the installed skills is
#: generic: it names a skill family (test, ci, a2a, merge, validation …; 5 %
#: of ~320 skills is 16 documents), so it cannot be the reason to pick one
#: member. ``GENERIC_DF_FLOOR`` keeps a small index (tests, a fresh node)
#: from calling every token generic.
GENERIC_DF_FRACTION = 0.05
GENERIC_DF_FLOOR = 3
#: A pure question names things without asking for work on them ("does the
#: nightly backup run on every node?"), so it needs one more distinctive
#: concept.
QUESTION_EXTRA_CONCEPTS = 1
#: Runners-up must reach this fraction of the best score.
RELATIVE_FLOOR = 0.7
#: Prompts shorter than this (after stripping) carry no routing signal.
MIN_PROMPT_CHARS = 12
#: Prompts longer than this are a pasted document or a generated instruction
#: block (a reviewer brief, a skill body, a continuation summary), not a
#: request: on the #2748 set every operator-typed prompt was under 2,600
#: characters and every machine-written one over 3,900.
MAX_PROMPT_CHARS = 3000
#: Health probes are short ASCII one-liners; longer text is never one.
PROBE_MAX_CHARS = 160
#: Korean synonym surfaces up to this many syllables match only at the start
#: of a word (stems lead, particles trail): "머지" must not fire inside
#: "나머지", nor "워드" inside "키워드". Longer surfaces match anywhere.
SHORT_SURFACE_CHARS = 2
#: Hard cap on the injected context (characters).
MAX_CONTEXT_CHARS = 720
#: Description excerpt per candidate.
DESC_EXCERPT_CHARS = 110
#: check-description: distinct content tokens a description needs.
MIN_DESCRIPTION_TOKENS = 6
#: check-description: the draft must beat the best installed skill by this factor.
SELF_RETRIEVAL_MARGIN = 1.10
#: BM25 parameters.
BM25_K1 = 1.2
BM25_B = 0.75
#: Name tokens count this many times in a document (the runtime sees the name).
NAME_TOKEN_WEIGHT = 2
#: Index cache is rebuilt when any root's listing changed or after this long.
INDEX_TTL_SECONDS = 24 * 3600
#: A SKILL.md larger than this is skipped (mirrors skill_lookup.MAX_SKILL_BYTES).
MAX_SKILL_BYTES = 128 * 1024

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_APPROVAL_WORDS = frozenset({"승인", "approve", "approved", "ok", "okay", "yes", "네", "응", "좋아", "진행", "go"})
_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those it its is are was were be been being
    to of in on at by for with from as into onto over under about after before during while when
    whenever where which who whom whose why how what use using used uses skill skills tool tools
    do does did done doing not no yes can could should would may might must will shall have has had
    you your yours we our ours they their them i me my mine he she his her one ones any all some
    also only just more most less least very too so such same other another each every both either
    neither via per etc eg ie e.g i.e always never ever often sometimes read write run get set
    make made making need needs needed want wants wanted like want help please show tell give
    ccc node nodes fleet
    """.split()
)

#: Runtime the ``suggest`` hook feeds when CCC_SKILL_ROUTER_RUNTIME is unset:
#: the hook is registered only in Claude Code's hooks (#2658).
DEFAULT_SUGGEST_RUNTIME = "claude"
#: Root labels offered to every runtime: the operator listed them explicitly.
SHARED_RUNTIMES = frozenset({"extra"})

STATE_SUBDIR = "skill-router"
SUGGESTIONS_FILE = "suggestions.jsonl"
INDEX_FILE = "index.json"
OFF_FILE = "skill-router.off"


# --- paths -------------------------------------------------------------------------


def claude_dir(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    explicit = (env.get("CCC_CLAUDE_DIR") or "").strip()
    if explicit:
        return explicit
    return os.path.join(env.get("HOME", "/root"), ".claude")


def state_dir(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    explicit = (env.get("CCC_STATE_DIR") or "").strip()
    base = explicit or os.path.join(claude_dir(env), "state")
    return os.path.join(base, STATE_SUBDIR)


def skill_roots(env: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """``[(runtime, root)]`` — the installed roots the runtime actually lists."""
    env = os.environ if env is None else env
    roots = [("claude", os.path.join(claude_dir(env), "skills"))]
    codex_home = (env.get("CODEX_HOME") or "").strip() or os.path.join(env.get("HOME", "/root"), ".codex")
    roots.append(("codex", os.path.join(codex_home, "skills")))
    extra = (env.get("CCC_SKILL_ROUTER_EXTRA_ROOTS") or "").strip()
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(("extra", part))
    return roots


def core_names(env: Optional[Dict[str, str]] = None) -> set:
    """Skills whose description is always in the listing (skill-listing-core.txt)."""
    env = os.environ if env is None else env
    explicit = (env.get("CCC_SKILL_LISTING_CORE") or "").strip()
    candidates = [explicit] if explicit else []
    candidates.append(os.path.join(claude_dir(env), "hooks", "skill-listing-core.txt"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "skill-listing-core.txt"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                names = set()
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#") and _NAME_RE.fullmatch(line):
                        names.add(line)
                return names
        except OSError:
            continue
    return set()


# --- synonyms ----------------------------------------------------------------------


def _synonyms_path(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    explicit = (env.get("CCC_SKILL_ROUTER_SYNONYMS") or "").strip()
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill-router-synonyms.txt")


def load_synonyms(path: Optional[str] = None) -> List[Tuple[str, List[str]]]:
    """``[(surface, [english tokens])]`` — ``surface => tok tok`` per line, ``#`` comments.

    Surfaces are matched as substrings of the lower-cased prompt (Korean is
    agglutinative: ``머지해줘`` must hit ``머지``), longest surface first so a
    longer phrase wins over a word inside it.
    """
    path = path or _synonyms_path()
    rows: List[Tuple[str, List[str]]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=>" not in line:
                    continue
                surface, _, targets = line.partition("=>")
                surface = surface.strip().lower()
                tokens = [t for t in _TOKEN_RE.findall(targets.lower()) if t not in _STOPWORDS]
                if surface and tokens:
                    rows.append((surface, tokens))
    except OSError:
        return []
    rows.sort(key=lambda row: -len(row[0]))
    return rows


# --- tokenization ------------------------------------------------------------------


def tokenize(text: str) -> List[str]:
    """Lower-case ASCII word tokens minus stopwords; kebab names split into parts."""
    out: List[str] = []
    for token in _TOKEN_RE.findall((text or "").lower().replace("-", " ").replace("_", " ").replace("/", " ")):
        token = token.strip(".")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        out.append(token)
    return out


def _is_hangul(char: str) -> bool:
    return "가" <= char <= "힣"


def _is_ascii_word(char: str) -> bool:
    return char.isascii() and char.isalnum()


def surface_matches(lowered: str, surface: str) -> bool:
    """Whether ``surface`` occurs in the lower-cased prompt as a word of its own.

    * an ASCII surface is a whole token: ``pr`` is not inside ``provider``,
      ``ci`` not inside ``decision`` (substring matching made both fire on
      most English text);
    * a Korean surface of up to ``SHORT_SURFACE_CHARS`` syllables must start
      a word — it may carry particles or endings (``머지해줘``) but not sit
      inside another word (``나머지``, ``키워드``);
    * longer surfaces match anywhere (compounds written without spaces).
    """
    ascii_surface = surface.isascii()
    start = lowered.find(surface)
    while start >= 0:
        end = start + len(surface)
        before = lowered[start - 1] if start else " "
        after = lowered[end] if end < len(lowered) else " "
        if ascii_surface:
            if not _is_ascii_word(before) and not _is_ascii_word(after):
                return True
        elif len(surface) > SHORT_SURFACE_CHARS or not _is_hangul(before):
            return True
        start = lowered.find(surface, start + 1)
    return False


def prompt_concepts(text: str, synonyms: Iterable[Tuple[str, List[str]]]) -> Tuple[List[str], Dict[str, str]]:
    """``(tokens, concept_of)`` — the prompt's tokens and the concept each belongs to.

    A concept is one thing the operator wrote: an ASCII word is its own
    concept, and the English targets of one synonym surface share that
    surface's concept (``테스트 => test tests`` is one). A target offered by
    several matched surfaces goes to the one holding the fewest so far, then
    the shortest: two words the operator wrote stay two concepts when their
    lines overlap (``머지 큐`` + ``머지`` + ``큐`` → merge, queue), and the
    count never exceeds the number of surfaces or of tokens.
    """
    lowered = (text or "").lower()
    tokens = tokenize(lowered)
    concept_of: Dict[str, str] = {token: token for token in tokens}
    offers: Dict[str, List[str]] = {}
    for surface, targets in synonyms:
        if surface_matches(lowered, surface):
            for target in targets:
                if target not in concept_of and surface not in offers.setdefault(target, []):
                    offers[target].append(surface)
    held: Dict[str, int] = {}
    for target, surfaces in sorted(offers.items(), key=lambda item: len(item[1])):
        surface = min(surfaces, key=lambda name: (held.get(name, 0), len(name)))
        held[surface] = held.get(surface, 0) + 1
        concept_of[target] = surface
        tokens.append(target)
    return tokens, concept_of


def expand_prompt(text: str, synonyms: Iterable[Tuple[str, List[str]]]) -> List[str]:
    """Prompt tokens plus English tokens for every synonym surface found in it."""
    return prompt_concepts(text, synonyms)[0]


# --- index -------------------------------------------------------------------------


def _frontmatter(text: str) -> Optional[Dict[str, str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    values: Dict[str, str] = {}
    for line in lines[1:200]:
        if line.strip() == "---":
            break
        if not line.strip() or line[0] in " \t-#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value and key not in values:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    else:
        return None
    if "name" not in values or "description" not in values:
        return None
    return values


def _root_signature(root: str) -> str:
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return "missing"
    try:
        mtime = int(os.stat(root).st_mtime)
    except OSError:
        mtime = 0
    digest = hashlib.sha1("\n".join(entries).encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{mtime}:{len(entries)}:{digest}"


def _read_skill(root: str, name: str) -> Optional[Dict[str, str]]:
    """Frontmatter of ``<root>/<name>/SKILL.md`` when it is an active, well-formed skill."""
    path = os.path.join(root, name, "SKILL.md")
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_SKILL_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read(16384)
    except OSError:
        return None
    fm = _frontmatter(text)
    if fm is None or fm.get("name") != name:
        return None
    if fm.get("status", "active") != "active":
        return None
    return fm


def scan_roots(roots: Iterable[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """One document per installed skill: name, runtime, runtimes, description, tokens.

    ``runtime`` is the first root that holds the name (scoring uses that copy);
    ``runtimes`` lists every root that holds it, so ``suggest`` can offer a
    skill to exactly the runtimes able to invoke it (#2658)."""
    docs: List[Dict[str, Any]] = []
    by_name: Dict[str, Dict[str, Any]] = {}
    for runtime, root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if not _NAME_RE.fullmatch(name):
                continue
            seen = by_name.get(name)
            if seen is not None and runtime in seen["runtimes"]:
                continue
            fm = _read_skill(root, name)
            if fm is None:
                continue
            if seen is not None:
                seen["runtimes"].append(runtime)
                continue
            tokens = tokenize(name) * NAME_TOKEN_WEIGHT + tokenize(fm["description"])
            if not tokens:
                continue
            doc = {
                "name": name, "runtime": runtime, "runtimes": [runtime],
                "description": " ".join(fm["description"].split())[:400], "tokens": tokens,
            }
            by_name[name] = doc
            docs.append(doc)
    return docs


def suggest_runtime(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Runtime whose skills ``suggest`` may offer, or None when it is not a known root."""
    env = os.environ if env is None else env
    runtime = (env.get("CCC_SKILL_ROUTER_RUNTIME") or "").strip().lower() or DEFAULT_SUGGEST_RUNTIME
    known = {label for label, _ in skill_roots(env)} - SHARED_RUNTIMES
    return runtime if runtime in known else None


def runtime_docs(index: Dict[str, Any], runtime: str) -> List[Dict[str, Any]]:
    """Index documents ``runtime`` can invoke: its own root plus the shared (extra) roots.

    Indexes cached before #2658 carry only ``runtime``; that single label is used then."""
    allowed = {runtime} | SHARED_RUNTIMES
    out = []
    for doc in index.get("docs") or []:
        labels = doc.get("runtimes")
        if not isinstance(labels, list) or not labels:
            labels = [doc.get("runtime")]
        if allowed.intersection(labels):
            out.append(doc)
    return out


def load_index(env: Optional[Dict[str, str]] = None, *, rebuild: bool = False) -> Dict[str, Any]:
    """Cached scan of the roots; rebuilt when a root listing changed or the TTL passed."""
    env = os.environ if env is None else env
    roots = skill_roots(env)
    signature = {root: _root_signature(root) for _, root in roots}
    path = os.path.join(state_dir(env), INDEX_FILE)
    if not rebuild:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
            if (
                isinstance(cached, dict)
                and cached.get("signature") == signature
                and time.time() - float(cached.get("built_at") or 0) < INDEX_TTL_SECONDS
                and isinstance(cached.get("docs"), list)
            ):
                return cached
        except (OSError, ValueError, TypeError):
            pass
    docs = scan_roots(roots)
    index = {"built_at": time.time(), "signature": signature, "docs": docs}
    _write_private(path, json.dumps(index, ensure_ascii=False))
    return index


def _write_private(path: str, text: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _append_private(path: str, line: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except OSError:
        return False


# --- scoring -----------------------------------------------------------------------


class Ranker:
    """BM25 over the index documents."""

    def __init__(self, docs: List[Dict[str, Any]]) -> None:
        self.docs = docs
        self.n = len(docs)
        self.df: Dict[str, int] = {}
        total = 0
        for doc in docs:
            tokens = doc.get("tokens") or []
            total += len(tokens)
            for token in set(tokens):
                self.df[token] = self.df.get(token, 0) + 1
        self.avgdl = (total / self.n) if self.n else 1.0

    def idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def is_generic(self, token: str) -> bool:
        """A token so common across the installed skills that it names a family."""
        return self.df.get(token, 0) > max(GENERIC_DF_FLOOR, GENERIC_DF_FRACTION * self.n)

    def is_noise(self, token: str) -> bool:
        """A number, or an id/date/hash no skill mentions: not a content word."""
        if token.replace(".", "").isdigit():
            return True
        return token not in self.df and any(char.isdigit() for char in token)

    def match(self, query_tokens: List[str], doc: Dict[str, Any], concept_of: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Score one document: ``score``, ``shared``, ``named``, ``name_size``, ``coverage``.

        Evidence is counted per prompt concept (``prompt_concepts``), not per
        token: a concept scores once, through its strongest token in the
        document. ``shared`` is the number of concepts found through a
        non-generic token (generic ones add score and coverage, never
        evidence); ``named`` how many of those hit one of the ``name_size``
        non-generic tokens of the skill's name. ``coverage`` is the IDF mass
        of the found concepts over the mass of the prompt's
        ``COVERAGE_WINDOW`` heaviest concepts (0..1): corpus-size independent
        and, with the window, prompt-length independent.
        """
        tokens = doc.get("tokens") or []
        if not tokens:
            return {"score": 0.0, "shared": 0, "named": 0, "name_size": 0, "coverage": 0.0, "focus": 0.0}
        concept_of = concept_of or {}
        name_tokens = {token for token in tokenize(str(doc.get("name") or "")) if not self.is_generic(token)}
        tf: Dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        norm = BM25_K1 * (1 - BM25_B + BM25_B * len(tokens) / self.avgdl)
        weight: Dict[str, Tuple[bool, float]] = {}  # concept -> (known, idf) of its heaviest token
        hit: Dict[str, Tuple[float, float]] = {}  # concept -> (bm25 part, idf) of its best hit
        shared = set()
        named = set()
        for token in set(query_tokens):
            if self.is_noise(token):
                continue
            concept = concept_of.get(token, token)
            idf = self.idf(token)
            # A translation that no skill uses must not outweigh one that some do.
            known = token in self.df
            seen = weight.get(concept)
            if seen is None or (known, idf) > seen:
                weight[concept] = (known, idf)
            freq = tf.get(token)
            if not freq:
                continue
            part = idf * freq * (BM25_K1 + 1) / (freq + norm)
            if concept not in hit or part > hit[concept][0]:
                hit[concept] = (part, idf)
            if not self.is_generic(token):
                shared.add(concept)
                if token in name_tokens:
                    named.add(concept)
        weights = sorted((idf for _, idf in weight.values()), reverse=True)
        mass = sum(weights)
        window = sum(weights[:COVERAGE_WINDOW])
        hit_mass = sum(idf for _, idf in hit.values())
        return {
            "score": sum(part for part, _ in hit.values()),
            "shared": len(shared),
            "named": len(named),
            "name_size": len(name_tokens),
            "coverage": min(1.0, hit_mass / window) if window else 0.0,
            "focus": hit_mass / mass if mass else 0.0,
        }

    def score(self, query_tokens: List[str], doc: Dict[str, Any]) -> Tuple[float, int, float]:
        """``(bm25, distinctive shared tokens, coverage)`` with every token its own concept."""
        found = self.match(query_tokens, doc)
        return found["score"], found["shared"], found["coverage"]

    def rank(self, query_tokens: List[str], *, exclude: Iterable[str] = (), concept_of: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        skip = set(exclude)
        rows = []
        for doc in self.docs:
            if doc["name"] in skip:
                continue
            found = self.match(query_tokens, doc, concept_of)
            if found["score"] > 0:
                rows.append({
                    "name": doc["name"], "runtime": doc["runtime"], "description": doc["description"],
                    "score": round(found["score"], 3), "shared": found["shared"], "named": found["named"],
                    "name_size": found["name_size"], "coverage": round(found["coverage"], 3), "focus": round(found["focus"], 3),
                })
        rows.sort(key=lambda row: (-row["score"], row["name"]))
        return rows


def _anchored(row: Dict[str, Any]) -> bool:
    """The match rests on the skill's name, or on a focused prompt full of its description.

    * name: ``MIN_NAME_CONCEPTS`` shared concepts are name words and the
      windowed coverage holds — the only way a long prompt matches;
    * description: ``MIN_DESCRIPTION_CONCEPTS`` shared concepts and the
      coverage holds over the *whole* prompt — a symptom told in the
      description's words, never a few words inside a pasted report.
    """
    if row["coverage"] < MIN_COVERAGE:
        return False
    if row["name_size"] and row["named"] >= min(MIN_NAME_CONCEPTS, row["name_size"]):
        return True
    return row["shared"] >= MIN_DESCRIPTION_CONCEPTS and row["focus"] >= MIN_COVERAGE


def candidates_for(
    prompt: str,
    index: Dict[str, Any],
    *,
    exclude: Iterable[str] = (),
    synonyms: Optional[List[Tuple[str, List[str]]]] = None,
    allowed: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Top candidates above the thresholds, or [].

    ``allowed`` (names the calling runtime can invoke, #2658) drops the other
    runtimes' skills *after* ranking: scores keep using the whole index so the
    thresholds stay calibrated (IDF over a one-skill root is near zero)."""
    synonyms = load_synonyms() if synonyms is None else synonyms
    tokens, concept_of = prompt_concepts(prompt, synonyms)
    if not tokens:
        return []
    ranker = Ranker(index.get("docs") or [])
    skip = set(exclude)
    keep = None if allowed is None else set(allowed)
    min_shared = MIN_SHARED_CONCEPTS + (QUESTION_EXTRA_CONCEPTS if is_pure_question(prompt) else 0)
    ranked = [
        row for row in ranker.rank(tokens, concept_of=concept_of)
        if (keep is None or row["name"] in keep)
        and row["score"] >= MIN_SCORE and row["shared"] >= min_shared and _anchored(row)
    ]
    if not ranked:
        return []
    # An excluded (core) skill that wins outright is already described in the
    # listing: the runtime will find it without help, and runners-up would be
    # noise. Only candidates comparable to that winner are worth a line.
    bar = ranked[0]["score"] * RELATIVE_FLOOR
    rows = [row for row in ranked if row["name"] not in skip and row["score"] >= bar]
    return rows[:MAX_CANDIDATES]


# --- hook: suggest -----------------------------------------------------------------


def _skip_reason(prompt: str) -> Optional[str]:
    text = (prompt or "").strip()
    if len(text) < MIN_PROMPT_CHARS:
        return "short"
    if len(text) > MAX_PROMPT_CHARS:
        return "long"
    if text.startswith("/"):
        return "slash-command"
    if _is_approval(_strip_tail(text)):
        return "approval"
    if _is_choice_echo(text):
        return "choice-echo"
    if _is_probe(text):
        return "probe"
    return None


#: A health probe tells the model what to reply, not what to do ("Reply with
#: exactly: pong", "Name two tools you can call."): bridges and doctors send
#: them, and their words ("name", "call", "single") are not a task.
_PROBE_LEADS = ("reply with", "respond with", "answer with", "answer in one", "say exactly", "say only")
_PROBE_MARKS = ("tool you can call", "tools you can call", "single word", "one word only")


def _is_probe(text: str) -> bool:
    if len(text) > PROBE_MAX_CHARS or not text.isascii():
        return False
    lowered = " ".join(text.lower().split())
    return lowered.startswith(_PROBE_LEADS) or any(mark in lowered for mark in _PROBE_MARKS)


#: Question forms that do ask for work: a proposal ("…할까?", "…없을까?",
#: "can we …?") or a how-to. Everything else ending in "?" is a question
#: about the state of things.
_REQUEST_QUESTION_ENDINGS = ("까", "없나", "되나", "하나", "줄래", "어때")
_REQUEST_QUESTION_LEADS = ("can you", "could you", "would you", "will you", "can we", "could we", "shall we", "how do", "how can", "how to")
_SENTENCE_END = ".!?"


def is_pure_question(prompt: str) -> bool:
    """Every sentence ends in ``?`` and none of them is a request in question form.

    Plain scan, no regex. A ``.``/``!`` only ends a sentence before white
    space (``run.py``, ``v1.2`` stay whole); a line break always does.
    """
    sentences: List[str] = []
    for line in (prompt or "").splitlines():
        start = 0
        for i, char in enumerate(line):
            if char in _SENTENCE_END and (i + 1 == len(line) or line[i + 1].isspace()):
                sentences.append(line[start : i + 1])
                start = i + 1
        sentences.append(line[start:])
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return False
    for sentence in sentences:
        if not sentence.endswith("?"):
            return False
        body = sentence.rstrip("?").rstrip().lower()
        if body.endswith(_REQUEST_QUESTION_ENDINGS) or body.startswith(_REQUEST_QUESTION_LEADS):
            return False
    return True


#: Separators after which an approval reply carries only the copied option
#: explanation ("2651 승인 — 두 PR 모두 …", "1 (권장)").
_TAIL_SEPARATORS = (" — ", " – ", " - ", " -- ", " : ")


def _strip_tail(text: str) -> str:
    """Drop a copied explanation tail: anything after an em/en dash or colon
    separator, then trailing parenthesised notes ("(권장)", "(recommended)").
    Plain string operations (no regex; see _is_approval)."""
    for sep in _TAIL_SEPARATORS:
        cut = text.find(sep)
        if cut > 0:
            text = text[:cut]
    text = text.strip()
    while text.endswith(")") and "(" in text:
        text = text[: text.rfind("(")].rstrip()
    return text


def _is_choice_echo(text: str) -> bool:
    """A reply that starts with an option marker — ``1.`` / ``2)`` / ``3 .`` —
    is the operator picking (and usually pasting) one of the assistant's
    numbered options. Its words are the assistant's own wording, so routing on
    them only echoes the previous turn back as noise (#2652)."""
    head = text.lstrip()[:5]
    digits = 0
    while digits < len(head) and head[digits].isdigit():
        digits += 1
    if digits == 0 or digits > 2:
        return False
    rest = head[digits:].lstrip(" ")
    return rest[:1] in (".", ")") and (len(rest) == 1 or rest[1:2] in (" ", "\t", ""))


def _is_approval(text: str) -> bool:
    """``1`` / ``2637 승인`` / ``1. 2. 승인`` / ``ok`` — numbers with optional
    trailing ``.``/``)`` and at most one closing approval word. Plain token
    scan, no regex: a nested-quantifier pattern here was flagged for
    exponential backtracking (CodeQL, #2645)."""
    words = text.lower().split()
    if not words or len(words) > 8:
        return False
    if words[-1] in _APPROVAL_WORDS:
        words = words[:-1]
    for word in words:
        core = word.rstrip(".)")
        if not core or not core.isdigit() or len(core) > 12:
            return False
    return True


def _excerpt(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def render_context(rows: List[Dict[str, Any]]) -> str:
    """The injected text: candidates only, never the prompt."""
    lines = ["🧭 ccc-node skill router — candidate skills for this request (pick only if it fits; core skills are already described in the listing):"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. `{row['name']}` — {_excerpt(row['description'], DESC_EXCERPT_CHARS)}")
    lines.append("Invoke with the Skill tool when one matches; ignore otherwise.")
    text = "\n".join(lines)
    while len(text) > MAX_CONTEXT_CHARS and len(lines) > 2:
        lines.pop(-2)
        text = "\n".join(lines)
    return text[:MAX_CONTEXT_CHARS]


def router_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    raw = (env.get("CCC_SKILL_ROUTER") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    off = os.path.join(os.path.dirname(state_dir(env)), OFF_FILE)
    return not os.path.exists(off)


def cmd_suggest(argv: List[str]) -> int:
    """UserPromptSubmit hook body: hook JSON on stdin, hookSpecificOutput (or nothing) on stdout."""
    env = os.environ
    if not router_enabled(env):
        return 0
    try:
        raw = sys.stdin.read(65536)
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not isinstance(prompt, str):
        return 0
    if _skip_reason(prompt):
        return 0
    runtime = suggest_runtime(env)
    if runtime is None:
        return 0
    session = str(payload.get("session_id") or "")[:128]
    session = re.sub(r"[^A-Za-z0-9_.-]", "", session)
    index = load_index(env)
    allowed = [doc["name"] for doc in runtime_docs(index, runtime)]
    rows = candidates_for(prompt, index, exclude=core_names(env), allowed=allowed)
    if not rows:
        return 0
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    names = [row["name"] for row in rows]
    record = {"ts": ts, "session": session, "candidates": [{"name": r["name"], "score": r["score"]} for r in rows]}
    sdir = state_dir(env)
    _append_private(os.path.join(sdir, SUGGESTIONS_FILE), json.dumps(record, ensure_ascii=False, sort_keys=True))
    if session:
        _write_private(os.path.join(sdir, f"last-{session}.json"), json.dumps({"ts": ts, "candidates": names}))
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": render_context(rows)}}, ensure_ascii=False) + "\n")
    return 0


# --- query (operator / tests) ------------------------------------------------------


def cmd_query(argv: List[str]) -> int:
    text = " ".join(a for a in argv if not a.startswith("--") and not a.isdigit())
    limit = _opt_int(argv, "limit", MAX_CANDIDATES)
    index = load_index()
    synonyms = load_synonyms()
    tokens, concept_of = prompt_concepts(text, synonyms)
    rows = Ranker(index.get("docs") or []).rank(tokens, concept_of=concept_of)[:limit]
    if "--json" in argv:
        sys.stdout.write(json.dumps({"tokens": tokens, "results": rows}, ensure_ascii=False) + "\n")
    else:
        for row in rows:
            sys.stdout.write(f"{row['score']:6.2f}  {row['shared']:2d}  {row['coverage']:.2f}  {row['name']}\n")
    return 0


# --- check-description (autoinstall gate) ------------------------------------------


def check_description(name: str, description: str, index: Dict[str, Any]) -> Dict[str, Any]:
    """Self-retrieval verdict for a draft; body-free (names and numbers only)."""
    distinct = len(set(tokenize(description)))
    result: Dict[str, Any] = {"name": name, "distinct_tokens": distinct, "ok": False}
    if distinct < MIN_DESCRIPTION_TOKENS:
        result["reason"] = "too-few-content-tokens"
        return result
    docs = [doc for doc in (index.get("docs") or []) if doc["name"] != name]
    self_doc = {"name": name, "runtime": "draft", "description": description, "tokens": tokenize(name) * NAME_TOKEN_WEIGHT + tokenize(description)}
    ranker = Ranker(docs + [self_doc])
    query = tokenize(description)
    self_score, _, _ = ranker.score(query, self_doc)
    best_other = None
    for doc in docs:
        score, shared, _ = ranker.score(query, doc)
        if best_other is None or score > best_other["score"]:
            best_other = {"name": doc["name"], "score": round(score, 3), "shared": shared}
    result["self_score"] = round(self_score, 3)
    result["best_other"] = best_other
    if best_other is not None and self_score < best_other["score"] * SELF_RETRIEVAL_MARGIN:
        result["reason"] = "shadowed-by-installed-skill"
        return result
    result["ok"] = True
    return result


def cmd_check_description(argv: List[str]) -> int:
    name = _opt(argv, "name").strip()
    description = _opt(argv, "description")
    if not description and "--stdin" in argv:
        description = sys.stdin.read(4096)
    if not name or not description.strip():
        sys.stderr.write("usage: skill_router.py check-description --name N --description D [--json]\n")
        return 2
    try:
        result = check_description(name, description, load_index())
    except Exception as exc:  # noqa: BLE001 - the gate reports unverifiable, never crashes
        sys.stderr.write(f"skill_router: check failed: {exc.__class__.__name__}\n")
        return 2
    if "--json" in argv:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        sys.stdout.write(("ok" if result["ok"] else f"fail {result.get('reason')}") + "\n")
    return 0 if result["ok"] else 1


# --- report ------------------------------------------------------------------------


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        pass
    return rows


def build_report(suggestions: List[Dict[str, Any]], usage: List[Dict[str, Any]], *, since: str) -> Dict[str, Any]:
    sug = [r for r in suggestions if str(r.get("ts") or "") >= since]
    use = [r for r in usage if str(r.get("ts") or "") >= since and r.get("tool") == "Skill"]
    suggested_counts: Dict[str, int] = {}
    for rec in sug:
        for cand in rec.get("candidates") or []:
            name = cand.get("name") if isinstance(cand, dict) else None
            if name:
                suggested_counts[name] = suggested_counts.get(name, 0) + 1
    used_counts: Dict[str, int] = {}
    hits = 0
    tagged = 0
    for rec in use:
        name = str(rec.get("skill") or "")
        used_counts[name] = used_counts.get(name, 0) + 1
        if "suggested" in rec:
            tagged += 1
            if rec.get("suggested") is True:
                hits += 1
    suggested_never_used = sorted(
        ((n, c) for n, c in suggested_counts.items() if used_counts.get(n, 0) == 0), key=lambda x: (-x[1], x[0])
    )[:10]
    used_never_suggested = sorted(
        ((n, c) for n, c in used_counts.items() if suggested_counts.get(n, 0) == 0), key=lambda x: (-x[1], x[0])
    )[:10]
    return {
        "since": since,
        "prompts_with_suggestions": len(sug),
        "skill_invocations": len(use),
        "invocations_tagged": tagged,
        "invocations_following_a_suggestion": hits,
        "hit_rate": round(hits / tagged, 3) if tagged else None,
        "distinct_suggested": len(suggested_counts),
        "distinct_used": len(used_counts),
        "suggested_never_used": suggested_never_used,
        "used_never_suggested": used_never_suggested,
    }


def cmd_report(argv: List[str]) -> int:
    days = _opt_int(argv, "days", 14)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    sdir = state_dir()
    usage_path = os.path.join(os.path.dirname(sdir), "skill-usage", "usage.jsonl")
    report = build_report(_read_jsonl(os.path.join(sdir, SUGGESTIONS_FILE)), _read_jsonl(usage_path), since=since)
    if "--json" in argv:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    sys.stdout.write(f"skill-router report (last {days}d, since {since})\n")
    sys.stdout.write(f"  prompts with suggestions : {report['prompts_with_suggestions']}\n")
    sys.stdout.write(f"  Skill invocations        : {report['skill_invocations']} (tagged {report['invocations_tagged']})\n")
    rate = report["hit_rate"]
    sys.stdout.write(f"  followed a suggestion    : {report['invocations_following_a_suggestion']}" + (f" ({rate:.0%})\n" if rate is not None else "\n"))
    sys.stdout.write(f"  distinct suggested/used  : {report['distinct_suggested']}/{report['distinct_used']}\n")
    if report["suggested_never_used"]:
        sys.stdout.write("  suggested, never used    : " + ", ".join(f"{n}×{c}" for n, c in report["suggested_never_used"]) + "\n")
    if report["used_never_suggested"]:
        sys.stdout.write("  used, never suggested    : " + ", ".join(f"{n}×{c}" for n, c in report["used_never_suggested"]) + "\n")
    return 0


# --- index (operator) --------------------------------------------------------------


def cmd_index(argv: List[str]) -> int:
    index = load_index(rebuild="--rebuild" in argv)
    docs = index.get("docs") or []
    by_runtime: Dict[str, int] = {}
    for doc in docs:
        by_runtime[doc["runtime"]] = by_runtime.get(doc["runtime"], 0) + 1
    summary = {"skills": len(docs), "by_runtime": by_runtime, "built_at": index.get("built_at"), "core_excluded": len(core_names())}
    if "--json" in argv:
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"index: {summary['skills']} skills {by_runtime} core-excluded={summary['core_excluded']}\n")
    return 0


# --- cli ---------------------------------------------------------------------------


def _opt(argv: List[str], name: str, default: str = "") -> str:
    flag = "--" + name
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return default


def _opt_int(argv: List[str], name: str, default: int) -> int:
    try:
        return max(1, int(_opt(argv, name, str(default))))
    except ValueError:
        return default


def main(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write(__doc__.split("Usage::", 1)[-1])
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "suggest":
        return cmd_suggest(rest)
    if cmd == "query":
        return cmd_query(rest)
    if cmd == "check-description":
        return cmd_check_description(rest)
    if cmd == "report":
        return cmd_report(rest)
    if cmd == "index":
        return cmd_index(rest)
    sys.stderr.write(f"skill_router: unknown command {cmd!r}\n")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - a hook must never break a turn
        raise SystemExit(0)
