#!/usr/bin/env bash
# Tests for ccc-skill-registry.py — hermetic fixture repos, no network, and no
# writes outside the suite's private TMP (#1338). The real repo's freshness is
# asserted separately by validate-harness running `validate` against ROOT.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REG="$HERE/ccc-skill-registry.py"
pass=0; fail=0
TMP="$(mktemp -d 2>/dev/null || mktemp -d -t ccc-skill-registry-test)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# A minimal but complete fixture repo: one skill per audience, one managed
# shared skill, classifications for every classified-root file. Always from
# scratch — a stale artifact or a leftover skill from an earlier case would
# otherwise leak across cases.
make_fixture() {
  local root="$1"
  rm -rf "$root"
  mkdir -p "$root/skills/shared/demo-skill" "$root/claude/skills/claude-skill" \
           "$root/codex/skills/codex-skill/agents" "$root/piri/skills/piri-skill" "$root/codex"
  cat > "$root/skills/shared/demo-skill/SKILL.md" <<'MD'
---
name: demo-skill
description: Do the demo procedure end to end whenever the operator asks for it.
---

# demo-skill

Step one.
Step two.
Step three.
MD
  cat > "$root/claude/skills/claude-skill/SKILL.md" <<'MD'
---
name: claude-skill
description: Claude-only procedure that references nested metadata blocks safely.
metadata:
  type: ccc-skill
---

# claude-skill

Body line.
MD
  cat > "$root/codex/skills/codex-skill/SKILL.md" <<'MD'
---
name: codex-skill
description: Runtime-clean Codex procedure with no harness-specific references.
---

# codex-skill

Body line.
MD
  printf 'name: ${codex-skill}\n' > "$root/codex/skills/codex-skill/agents/openai.yaml"
  cat > "$root/piri/skills/piri-skill/SKILL.md" <<'MD'
---
name: piri-skill
description: Piri-native procedure outside the compatibility catalog entirely.
---

# piri-skill

Body line.
MD
  cat > "$root/codex/compatibility.json" <<'JSON'
{
  "schema_version": 1,
  "classifications": [
    {"pattern": "skills/shared/demo-skill/**", "compatibility": "shared"},
    {"pattern": "claude/skills/claude-skill/**", "compatibility": "claude-only"}
  ],
  "managed_skills": [
    {"name": "demo-skill", "source": "skills/shared/demo-skill"}
  ]
}
JSON
}

fixture_ok() { make_fixture "$TMP/fx"; }

# 1) render is deterministic
fixture_ok
python3 "$REG" render --repo-root "$TMP/fx" > "$TMP/r1.json" 2>"$TMP/r1.err"; rc1=$?
ok "render exits 0 on a valid fixture" 'test "$rc1" = 0'
python3 "$REG" render --repo-root "$TMP/fx" > "$TMP/r2.json" 2>/dev/null
ok "render is byte-deterministic" 'cmp -s "$TMP/r1.json" "$TMP/r2.json"'
ok "render lists all four skills" 'test "$(jq ".skills | length" "$TMP/r1.json")" = 4'
ok "managed shared skill is flagged" 'jq -e ".skills[] | select(.name == \"demo-skill\" and .managed == true and .classification == \"shared\")" "$TMP/r1.json" >/dev/null'
ok "piri skill carries no classification" 'jq -e ".skills[] | select(.name == \"piri-skill\" and .classification == null)" "$TMP/r1.json" >/dev/null'

# 2) update writes a fresh artifact atomically; a second run is a no-op
fixture_ok
out="$(python3 "$REG" update --repo-root "$TMP/fx")"
ok "update writes the artifact" 'echo "$out" | jq -e ".ok == true and .written == true" >/dev/null'
ok "artifact mode is 0644" 'test "$(stat -c %a "$TMP/fx/skills/registry.json")" = 644'
out2="$(python3 "$REG" update --repo-root "$TMP/fx")"
ok "second update is byte-idempotent (written=false)" 'echo "$out2" | jq -e ".written == false" >/dev/null'
ok "validate passes on a fresh fixture" 'python3 "$REG" validate --repo-root "$TMP/fx" | jq -e ".ok == true" >/dev/null'

# 3) content drift is detected as registry_stale
make_fixture "$TMP/stale"
python3 "$REG" update --repo-root "$TMP/stale" >/dev/null
printf '\nnew content\n' >> "$TMP/stale/skills/shared/demo-skill/SKILL.md"
err="$(python3 "$REG" validate --repo-root "$TMP/stale" 2>&1 >/dev/null)"
rc=$?
ok "stale tree fails validation" 'test "$rc" = 2'
ok "stale failure names registry_stale" 'grep -q "registry_stale" <<<"$err"'

# 4) an unclassified shared skill fails closed and update refuses to write
fixture_ok   # fresh fixture: no artifact exists yet, so refusal = nothing written
mkdir -p "$TMP/fx/skills/shared/other-skill"
cat > "$TMP/fx/skills/shared/other-skill/SKILL.md" <<'MD'
---
name: other-skill
description: An unclassified skill that must fail catalog validation.
---

# other-skill

Body line.
MD
err="$(python3 "$REG" render --repo-root "$TMP/fx" 2>&1 >/dev/null)"
ok "unclassified skill fails render" 'grep -q "registry_unclassified" <<<"$err"'
ok "update refuses an invalid tree" '! python3 "$REG" update --repo-root "$TMP/fx" >/dev/null 2>&1'
ok "no artifact written over an invalid tree" 'test ! -e "$TMP/fx/skills/registry.json"'

# 5) an unknown lifecycle status is rejected
make_fixture "$TMP/badstatus"
sed -i 's/^name: claude-skill$/name: claude-skill\nstatus: archived/' "$TMP/badstatus/claude/skills/claude-skill/SKILL.md"
err="$(python3 "$REG" render --repo-root "$TMP/badstatus" 2>&1 >/dev/null)"
ok "unknown status fails closed" 'grep -q "registry_status_invalid" <<<"$err"'

# 6) a deprecated skill stays in the registry with its lifecycle state
make_fixture "$TMP/deprecated"
sed -i 's/^name: claude-skill$/name: claude-skill\nstatus: deprecated/' "$TMP/deprecated/claude/skills/claude-skill/SKILL.md"
python3 "$REG" render --repo-root "$TMP/deprecated" > "$TMP/dep.json" 2>/dev/null
ok "deprecated skill renders with status=deprecated" 'jq -e ".skills[] | select(.name == \"claude-skill\" and .status == \"deprecated\")" "$TMP/dep.json" >/dev/null'
out="$(python3 "$REG" update --repo-root "$TMP/deprecated" 2>/dev/null)"
ok "deprecated tree is still writable" 'echo "$out" | jq -e ".ok == true" >/dev/null'

# 7) a managed entry whose source is missing fails closed
make_fixture "$TMP/orphan"
python3 - "$TMP/orphan/codex/compatibility.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["managed_skills"].append({"name": "ghost-skill", "source": "skills/shared/ghost-skill"})
json.dump(data, open(path, "w"))
PY
err="$(python3 "$REG" render --repo-root "$TMP/orphan" 2>&1 >/dev/null)"
ok "unknown managed source fails closed" 'grep -q "registry_managed_unknown" <<<"$err"'

# 8) git mode: tracked files come from git's view, a new untracked skill dir
#    still registers via the per-dir walk fallback, and a stray untracked file
#    beside a tracked skill never enters the hashes.
make_fixture "$TMP/gitmode"
git init -q "$TMP/gitmode" \
  && git -C "$TMP/gitmode" add -A \
  && git -C "$TMP/gitmode" -c user.email=t@local -c user.name=t commit -qm init
ok "git fixture initialized" 'test -d "$TMP/gitmode/.git"'
python3 "$REG" update --repo-root "$TMP/gitmode" >/dev/null 2>&1
printf 'stray\n' > "$TMP/gitmode/skills/shared/demo-skill/stray-file.txt"
out="$(python3 "$REG" update --repo-root "$TMP/gitmode" 2>/dev/null)"
ok "stray file beside tracked skill does not change hashes" 'echo "$out" | jq -e ".ok == true and .written == false" >/dev/null'
mkdir -p "$TMP/gitmode/skills/shared/new-skill"
cat > "$TMP/gitmode/skills/shared/new-skill/SKILL.md" <<'MD'
---
name: new-skill
description: Brand-new untracked skill that must register before git add.
---

# new-skill

Body line.
MD
python3 - "$TMP/gitmode/codex/compatibility.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["classifications"].append(
    {"pattern": "skills/shared/new-skill/**", "compatibility": "shared"}
)
json.dump(data, open(path, "w"))
PY
out="$(python3 "$REG" update --repo-root "$TMP/gitmode" 2>/dev/null)"
ok "untracked new skill dir registers in git mode" 'echo "$out" | jq -e ".ok == true and .written == true and .skills == 5" >/dev/null'
ok "git-mode tree validates fresh" 'python3 "$REG" validate --repo-root "$TMP/gitmode" | jq -e ".ok == true" >/dev/null'

echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
