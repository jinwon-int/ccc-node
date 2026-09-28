"""YAML-safe single-line SKILL.md frontmatter render/unquote (#2032).

fleet-skills validate.py (fleet-skills#328) parses SKILL.md frontmatter with
PyYAML and rejects unquoted descriptions YAML reads differently, while every
ccc-node reader still parses it line by line. These tests pin that the shared
renderer produces one line both kinds of reader agree on, with and without
PyYAML on the node.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from telegram_bot.utils import skill_frontmatter as sf

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "bridge" / "utils" / "skill_frontmatter.py"

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is optional on nodes and CI
    yaml = None

# The unquoted descriptions that broke fleet-skills #326/#323/#262/#256/#252
# under the #328 gate (abridged), plus the " #" comment-truncation shape.
REAL_FAILURES = [
    "Validate a Cloudflare API token's active status and permission groups. Use when "
    "invalidating cache after content deploys. Triggers: Cloudflare cache purge, purge by URL.",
    "Cluster heterogeneous async task failures across a fleet by duration and error signature. "
    "Use when one rollout fails differently on many nodes. Trigger keywords: fleet-wide failures, "
    "timeout cluster.",
    "Approve a GitHub PR from a second fleet account. Use when relay routing fails with errors "
    "like `ssh: Could not resolve hostname relay` or a missing ssh alias.",
    "Use when unit tests pass but invariants fail on production data. Triggers: production "
    "backup, invariant test, retention.",
    "Use when X: do Y",
    "Use when a PR fixes issue #42 and the changelog must follow",
]

CASES = REAL_FAILURES + [
    "Use when checking a plain description with no YAML hazards at all",
    "- starts with a sequence indicator",
    "? starts with a mapping key indicator",
    "[flow] sequence start",
    "{flow} mapping start",
    "# looks like a comment",
    "& anchor start",
    "* alias start",
    "! tag start",
    "| literal start",
    "> folded start",
    "'single quote start",
    '"double quote start',
    "% directive start",
    "@ reserved start",
    "`backtick` reserved start",
    "ends with a colon:",
    "  leading and trailing spaces  ",
    "yes",
    "No",
    "null",
    "~",
    "3 steps to recover a node",
    "2026-09-28 release notes",
    "1:20 sexagesimal",
    ".inf",
    "=",
    'contains "inner" quotes and a \\ backslash',
    "tab\tinside",
    "control\x07bell and \x1b escape and \x7f del and \x9f c1",
    "bom\ufeffinside and nel\x85 and ls\u2028 and ps\u2029",
    "emoji 😀 and 한국어 설명: 콜론",
    "colon:without space stays plain",
    "hash#without space stays plain",
]


def _yaml_value(raw: str):
    data = yaml.safe_load(f"description: {raw}\n")
    assert isinstance(data, dict) and set(data) == {"description"}
    return data["description"]


@pytest.fixture(params=["with-pyyaml", "without-pyyaml"])
def renderer(request, monkeypatch):
    """The helper as loaded on a node with, and on one without, PyYAML."""
    if request.param == "without-pyyaml":
        monkeypatch.setattr(sf, "_yaml", None)
    elif sf._yaml is None:
        pytest.skip("PyYAML not installed")
    return sf


@pytest.mark.parametrize("value", CASES)
def test_render_is_single_line_and_round_trips(renderer, value):
    rendered = renderer.render_scalar(value)
    expected = renderer.collapse(value)
    assert "\n" not in rendered and "\r" not in rendered
    assert renderer.unquote_scalar(rendered) == expected
    assert renderer.is_yaml_safe(rendered)
    # Idempotent: re-rendering the decoded value yields the same line.
    assert renderer.render_scalar(renderer.unquote_scalar(rendered)) == rendered


@pytest.mark.skipif(yaml is None, reason="PyYAML not installed")
@pytest.mark.parametrize("pyyaml_during_render", [True, False])
@pytest.mark.parametrize("value", CASES)
def test_rendered_value_is_what_pyyaml_reads(monkeypatch, value, pyyaml_during_render):
    if not pyyaml_during_render:
        monkeypatch.setattr(sf, "_yaml", None)
    rendered = sf.render_scalar(value)
    assert _yaml_value(rendered) == sf.collapse(value)


@pytest.mark.parametrize("value", REAL_FAILURES)
def test_real_failures_are_quoted_and_flagged_unsafe_unquoted(renderer, value):
    assert renderer.needs_quoting(value)
    assert renderer.render_scalar(value).startswith('"')
    assert not renderer.is_yaml_safe(value)


@pytest.mark.skipif(yaml is None, reason="PyYAML not installed")
def test_real_failures_really_break_pyyaml_unquoted():
    for value in REAL_FAILURES:
        try:
            parsed = _yaml_value(value)
        except yaml.YAMLError:
            continue
        assert parsed != value  # " #" truncation


def test_plain_values_stay_plain(renderer):
    value = "Use when checking a plain description with no YAML hazards at all"
    assert renderer.render_scalar(value) == value
    assert renderer.render_scalar("colon:without space stays plain") == "colon:without space stays plain"


def test_newlines_collapse_to_one_space(renderer):
    assert renderer.render_scalar("first line\n  second line\r\nthird") == "first line second line third"


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ('"Use when X: do Y"', "Use when X: do Y"),
        ('"say \\"hi\\" and C:\\\\path"', 'say "hi" and C:\\path'),
        ('"tab\\tand \\x41 \\u00e9 \\U0001F600 \\/ \\e"', "tab\tand A \u00e9 \U0001F600 / \x1b"),
        ("'it''s single-quoted'", "it's single-quoted"),
        ('"quoted"   # trailing comment', "quoted"),
        ("'quoted' # trailing comment", "quoted"),
        ("plain value # kept verbatim", "plain value # kept verbatim"),
        ('  "padded raw"  ', "padded raw"),
        ('"unterminated', '"unterminated'),
        ('"bad \\q escape"', '"bad \\q escape"'),
        ('"closed" trailing junk', '"closed" trailing junk'),
        ("", ""),
    ],
)
def test_unquote(raw, value):
    assert sf.unquote_scalar(raw) == value


def test_parse_scalar_reports_malformed_quotes():
    assert sf.parse_scalar('"unterminated') == ('"unterminated', False)
    assert sf.parse_scalar('"ok"') == ("ok", True)
    assert not sf.is_yaml_safe('"unterminated')
    assert not sf.is_yaml_safe(">-")
    assert not sf.is_yaml_safe("")


@pytest.mark.skipif(yaml is None, reason="PyYAML not installed")
@pytest.mark.parametrize(
    "raw",
    ['"Use when X: do Y"', "'it''s single-quoted'", '"a \\x41 \\u00e9"', '"quoted" # comment'],
)
def test_unquote_matches_pyyaml_for_quoted_scalars(raw):
    assert sf.unquote_scalar(raw) == _yaml_value(raw)


SKILL_MD = (
    "---\n"
    "name: demo-skill\n"
    "description: Use when X: do Y and fix issue #42\n"
    "---\n"
    "# Demo\n\nStep one.\nStep two.\n"
)


def test_normalize_skill_md_quotes_only_the_description_line(renderer):
    out = renderer.normalize_skill_md(SKILL_MD)
    lines = out.splitlines()
    assert lines[1] == "name: demo-skill"
    assert lines[2] == 'description: "Use when X: do Y and fix issue #42"'
    assert out.split("---\n", 2)[2] == SKILL_MD.split("---\n", 2)[2]
    assert renderer.normalize_skill_md(out) == out


@pytest.mark.skipif(yaml is None, reason="PyYAML not installed")
def test_normalized_frontmatter_passes_pyyaml_like_fleet_skills():
    out = sf.normalize_skill_md(SKILL_MD)
    lines = out.splitlines()
    end = lines.index("---", 1)
    data = yaml.safe_load("\n".join(lines[1:end]))
    assert data == {"name": "demo-skill", "description": "Use when X: do Y and fix issue #42"}
    # Line-reader view (fleet-skills raw check, ccc-node installers) agrees.
    raw = lines[2].partition(":")[2].strip()
    assert sf.unquote_scalar(raw) == data["description"]


def test_normalize_leaves_block_scalars_crlf_and_missing_frontmatter():
    block = "---\nname: x\ndescription: >-\n  folded text\n---\nbody\n"
    assert sf.normalize_skill_md(block) == block
    crlf = "---\r\nname: x\r\ndescription: a: b\r\n---\r\nbody\r\n"
    assert sf.normalize_skill_md(crlf) == '---\r\nname: x\r\ndescription: "a: b"\r\n---\r\nbody\r\n'
    assert sf.normalize_skill_md("no frontmatter: here\n") == "no frontmatter: here\n"
    body_only = "---\nname: x\ndescription: a: b\nno closing fence\n"
    assert sf.normalize_skill_md(body_only) == body_only


def test_cli_normalize_render_unquote(tmp_path):
    source = tmp_path / "SKILL.md"
    source.write_text(SKILL_MD, encoding="utf-8")
    target = tmp_path / "out.md"
    env = {**os.environ, "PYTHONPATH": ""}
    run = [sys.executable, str(MODULE_PATH)]
    assert subprocess.run([*run, "normalize", str(source), str(target)], env=env).returncode == 0
    assert target.read_text(encoding="utf-8") == sf.normalize_skill_md(SKILL_MD)
    assert source.read_text(encoding="utf-8") == SKILL_MD
    assert subprocess.run([*run, "normalize", str(source)], env=env).returncode == 0
    assert source.read_text(encoding="utf-8") == target.read_text(encoding="utf-8")
    rendered = subprocess.run(
        [*run, "render"], input="Use when X: do Y\n", capture_output=True, text=True, env=env
    )
    assert rendered.stdout == '"Use when X: do Y"'
    decoded = subprocess.run(
        [*run, "unquote"], input='"Use when X: do Y"\n', capture_output=True, text=True, env=env
    )
    assert decoded.stdout == "Use when X: do Y"
    assert subprocess.run([*run, "normalize", str(tmp_path / "missing")], env=env).returncode == 2
    assert subprocess.run([*run, "bogus"], env=env, capture_output=True).returncode == 64


def test_scripts_adapter_reexports_the_canonical_module():
    adapter_path = REPO_ROOT / "scripts" / "ccc_skill_frontmatter.py"
    spec = importlib.util.spec_from_file_location("ccc_skill_frontmatter_adapter_test", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    public = {
        name
        for name, value in vars(sf).items()
        if inspect.isfunction(value) and value.__module__ == sf.__name__ and not name.startswith("_")
    }
    assert public and public <= set(vars(adapter))
    assert adapter.render_scalar("Use when X: do Y") == sf.render_scalar("Use when X: do Y")


def test_bridge_line_readers_decode_quoted_values():
    from telegram_bot.core import skill_command, skill_lookup

    text = (
        "---\n"
        'name: "demo-skill"\n'
        'description: "Use when X: do Y and fix issue #42"\n'
        "---\nbody\n"
    )
    values = skill_lookup._frontmatter(text.replace('"demo-skill"', "demo-skill"))
    assert values is not None
    assert values["description"] == "Use when X: do Y and fix issue #42"
    assert skill_command._frontmatter_name(text) == "demo-skill"
    assert skill_command._frontmatter_name("---\nname: 'demo-skill'\n---\n") == "demo-skill"
