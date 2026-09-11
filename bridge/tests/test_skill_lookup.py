"""skill_lookup contract tests (#1678): approved roots, fail-closed reads,
deterministic search, revision staleness, and node-policy denial.

The synthetic world (repo registry + installed roots) lives in
``skill_lookup_fixtures`` and is wired through the shared ``lookup_env``
fixture in conftest; the real-repo parity test pins the staleness contract
against the CI-enforced registry.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from telegram_bot.core import skill_lookup
from telegram_bot.core.skill_lookup import (
    MAX_LIMIT,
    SkillLookupError,
    policy_denial,
    read,
    repo_tree_revision,
    search,
)
from skill_lookup_fixtures import write_skill


def _ids(payload: dict) -> list[str]:
    return [result["skill_id"] for result in payload["results"]]


def test_search_matches_name_and_description_deterministically(lookup_env) -> None:
    payload = search("fixture")
    assert _ids(payload) == [
        "repo:codex/skills/web",
        "repo:skills/web",
        "claude:local-one",
        "codex:codex-only",
        "piri:piri-only",
    ]
    assert payload["truncated"] is False
    first = payload["results"][0]
    assert first["active"] is True
    assert first["audience"] == "codex"
    assert len(first["revision"]) == 64


def test_search_runtime_filter_and_limit(lookup_env) -> None:
    assert _ids(search("fixture", runtime="claude")) == ["claude:local-one"]
    payload = search("fixture", limit=2)
    assert _ids(payload) == ["repo:codex/skills/web", "repo:skills/web"]
    assert payload["truncated"] is True
    assert search("fixture", limit=MAX_LIMIT)["truncated"] is False


def test_search_excludes_deprecated_and_requires_valid_input(lookup_env) -> None:
    assert _ids(search("deprecated fixture")) == []
    for query, runtime, limit in [
        ("", None, None),
        (" " * 300, None, None),
        ("x", "bogus", None),
        ("x", None, 0),
        ("x", None, MAX_LIMIT + 1),
    ]:
        with pytest.raises(SkillLookupError) as exc:
            search(query, runtime, limit)
        assert exc.value.code == "invalid_query"


def test_read_repo_entry_and_expected_revision(lookup_env) -> None:
    payload = search("fixture", runtime="repo")
    entry = payload["results"][1]  # repo:skills/web
    result = read(entry["skill_id"], entry["revision"])
    assert result["name"] == "web"
    assert result["bytes"] > 0
    assert "Fixture" in result["body"]
    with pytest.raises(SkillLookupError) as exc:
        read(entry["skill_id"], "0" * 64)
    assert exc.value.code == "stale_revision"


def test_read_installed_entry(lookup_env) -> None:
    result = read("claude:local-one")
    assert result["runtime"] == "claude"
    assert len(result["revision"]) == 64
    assert "fixture skill" in result["body"].lower()


def test_read_detects_repo_content_drift_against_registry(
    lookup_env, tmp_path: Path
) -> None:
    skill_md = tmp_path / "repo" / "skills" / "web" / "SKILL.md"
    # Overwrite with drifted-but-valid content for the same skill name.
    skill_md.write_text(
        "---\nname: web\ndescription: Fixture repo skill for lookup tests\n---\n\nlocal drift\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillLookupError) as exc:
        read("repo:skills/web")
    assert exc.value.code == "stale_revision"
    # The advertised registry revision stays in the error for re-search.
    assert len(exc.value.details["revision"]) == 64


def test_read_rejects_invalid_ids_and_missing_skills(lookup_env) -> None:
    for skill_id in [
        "repo",
        "repo:../escape",
        "claude:Upper",
        "bogus:x",
        "claude:no-such-skill",
        "repo:skills/no-such",
    ]:
        with pytest.raises(SkillLookupError) as exc:
            read(skill_id)
        assert exc.value.code in ("invalid_skill_id", "not_found")


def test_read_rejects_symlinked_skill(lookup_env, tmp_path: Path) -> None:
    skills = tmp_path / "home" / ".claude" / "skills"
    write_skill(tmp_path, "target")
    (skills / "evil").symlink_to(tmp_path / "target")
    with pytest.raises(SkillLookupError) as exc:
        read("claude:evil")
    assert exc.value.code == "unsafe_skill"


def test_read_rejects_group_writable_skill(lookup_env, tmp_path: Path) -> None:
    skill_md = tmp_path / "home" / ".claude" / "skills" / "local-one" / "SKILL.md"
    os.chmod(skill_md, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    with pytest.raises(SkillLookupError) as exc:
        read("claude:local-one")
    assert exc.value.code == "unsafe_skill"


def test_read_rejects_invalid_content(lookup_env, tmp_path: Path) -> None:
    skill_md = tmp_path / "home" / ".claude" / "skills" / "local-one" / "SKILL.md"
    skill_md.write_bytes(b"\xff\xfe\x00not utf8")
    with pytest.raises(SkillLookupError) as exc:
        read("claude:local-one")
    assert exc.value.code == "invalid_content"


def test_registry_fail_closed(lookup_env, tmp_path: Path) -> None:
    registry_path = tmp_path / "repo" / "skills" / "registry.json"
    original = json.loads(registry_path.read_text(encoding="utf-8"))
    for broken in [
        {"schema_version": 2, "skills": []},
        {"schema_version": 1, "skills": "nope"},
        {"schema_version": 1, "skills": [{"name": "web"}]},
        {
            "schema_version": 1,
            "skills": [
                {**original["skills"][0], "source": "../outside/escape"},
            ],
        },
    ]:
        registry_path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(SkillLookupError) as exc:
            read("repo:skills/web")
        assert exc.value.code == "registry_invalid"


def test_policy_denial_matrix() -> None:
    assert policy_denial({}) is None
    assert policy_denial({"CCC_NODE_ISOLATION_PROFILE": "fleet"}) is None
    assert policy_denial({"CCC_MEMORY_AUDIENCE": "private"}) is None
    assert policy_denial({"CCC_NODE_ISOLATION_PROFILE": "external"}) is not None
    assert policy_denial({"CCC_MEMORY_AUDIENCE": "shared"}) is not None
    assert policy_denial({"CCC_MEMORY_AUDIENCE": "surprise"}) is not None
    assert policy_denial({"CCC_NODE_ISOLATION_PROFILE": "surprise"}) is not None


def test_policy_denied_at_every_entrypoint(lookup_env, monkeypatch) -> None:
    monkeypatch.setenv("CCC_NODE_ISOLATION_PROFILE", "external")
    with pytest.raises(SkillLookupError) as exc:
        search("fixture")
    assert exc.value.code == "policy_denied"
    with pytest.raises(SkillLookupError):
        read("claude:local-one")


def test_real_registry_revisions_match_worktree() -> None:
    """Staleness contract against the CI-enforced registry (#1678)."""

    repo = skill_lookup.repo_root()
    registry_path = repo / "skills" / "registry.json"
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    for entry in document["skills"]:
        skill_dir = repo.joinpath(*entry["source"].split("/"))
        assert repo_tree_revision(skill_dir, repo) == entry["tree_sha256"], entry["source"]
