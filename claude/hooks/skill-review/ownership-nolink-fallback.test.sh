#!/usr/bin/env bash
# No-os.link fallback: exclusive publish must work where hardlinks are
# unavailable (Termux/Android), otherwise every gated autoinstall fails
# closed at provenance-write and stacks unprocessed pending drafts.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL="$HERE/ownership.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0
fail=0

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

STATE="$TMP/state"
SKILLS="$TMP/skills"
mkdir -m 700 "$STATE" "$SKILLS"

# Interpreter shim: hide os.link the way Termux/Android Python does.
SHIM="$TMP/shim"
mkdir -m 700 "$SHIM"
cat > "$SHIM/sitecustomize.py" <<'PY'
import os

os.link = None
PY

tool_nolink() {
  PYTHONPATH="$SHIM" python3 "$TOOL" --provider claude --skills-dir "$SKILLS" --state-dir "$STATE" "$@"
}

make_skill() {
  local name="$1"
  mkdir -m 700 "$SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for no-link fallback tests.\n---\n\n# %s\n\n## Steps\n1. Read.\n2. Verify.\n3. Record.\n' "$name" "$name" > "$SKILLS/$name/SKILL.md"
  chmod 600 "$SKILLS/$name/SKILL.md"
}

# 1. Unit: _publish_exclusive publishes via rename and still refuses clobber.
out="$(PYTHONPATH="$SHIM" python3 - "$TOOL" "$TMP" <<'PY'
import importlib.util
import os
import sys

tool = sys.argv[1]
tmp = sys.argv[2]
spec = importlib.util.spec_from_file_location("ownership", tool)
mod = importlib.util.module_from_spec(spec)
sys.modules["ownership"] = mod
spec.loader.exec_module(mod)

work = os.path.join(tmp, "unit")
os.makedirs(work, mode=0o700)
dir_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
fd = os.open(".src", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
os.write(fd, b"payload")
os.close(fd)
mod._publish_exclusive(".src", "dst", dir_fd, already_exists="exists_error")
assert open(os.path.join(work, "dst")).read() == "payload"
assert not os.path.exists(os.path.join(work, ".src"))
fd = os.open(".src2", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
os.write(fd, b"other")
os.close(fd)
try:
    mod._publish_exclusive(".src2", "dst", dir_fd, already_exists="exists_error")
except mod.ContractError as error:
    assert str(error) == "exists_error", str(error)
else:
    raise AssertionError("clobber was allowed")
print("unit-ok")
PY
)"
ok "rename fallback publishes and refuses clobber" '[ "$out" = "unit-ok" ]'

# 2. Integration: mark-created installs end-to-end without os.link.
make_skill nolink-one
out="$(tool_nolink mark-created nolink-one)"
ok "mark-created succeeds without os.link" 'jq -e ".ok == true" >/dev/null <<<"$out"'
ok "marker exists after fallback install" '[ -f "$SKILLS/nolink-one/.autosave-meta.json" ]'
ok "marker is valid autosave provenance" 'jq -e ".ownership == \"autosave-managed\" and .name == \"nolink-one\"" >/dev/null < "$SKILLS/nolink-one/.autosave-meta.json"'
ok "no temporary marker left behind" '! ls "$SKILLS/nolink-one"/.autosave-meta.json.tmp.* >/dev/null 2>&1'
ok "ledger records the create transaction" 'jq -s -e "[.[] | select(.event == \"create\" and .name == \"nolink-one\")] | group_by(.transaction_id) | any(map(.outcome) == [\"prepared\", \"changed\"])" "$STATE/skill-autosave-ownership.jsonl" >/dev/null'

# 3. Exclusive semantics preserved: pre-existing marker is refused.
make_skill nolink-two
printf '{"schema_version":2}\n' > "$SKILLS/nolink-two/.autosave-meta.json"
chmod 600 "$SKILLS/nolink-two/.autosave-meta.json"
out="$(tool_nolink mark-created nolink-two)"
ok "pre-existing marker still fails closed" 'jq -e ".ok == false" >/dev/null <<<"$out"'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
