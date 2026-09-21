"""Matrix transport state (#1780 PR-2a): admission policy, private store, config, operator block.

Ported from the family-messenger pilot's ``test_fleet_core`` /
``test_fleet_matrix_state`` and the state half of ``test_fleet_matrix``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from telegram_bot.core.matrix import state as m
from telegram_bot.core.matrix.state import (
    BLOCK_KEYS,
    MAX_REPLY_BYTES,
    OPERATOR_ACK_TEXT,
    SAFETY_STOP_REASONS,
    MatrixStore,
    Policy,
    QueueFull,
    Request,
    SafetyStop,
    Store,
    bounded_text,
    family_config,
    identities,
    mention_aliases,
    load_config,
    operator_name,
    parts,
    saved_policy,
    turn_id,
    turn_timeout_minutes,
    upgrade_saved_policy,
    validate_config,
    wake_words,
)

BOT = "@agent:example.test"
OWNER = "@owner:example.test"
OTHER = "@other:example.test"
ROOM = "!private:example.test"
GROUP = "!group:example.test"
NOW = 2_000_000_000
SCOPE = hashlib.sha256(json.dumps([BOT, ROOM, OWNER]).encode()).hexdigest()
FOREIGN_SCOPE = "f" * 64


def policy(**kwargs: Any) -> Policy:
    base: dict[str, Any] = {
        "account": BOT,
        "users": {OWNER, OTHER},
        "bots": {BOT},
        "rooms": {ROOM: "direct", GROUP: "mention"},
        "not_before_ms": NOW - 1000,
    }
    return Policy(**(base | kwargs))


def event(event_id: str = "$one", sender: str = OWNER) -> dict[str, Any]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": NOW,
        "content": {"msgtype": "m.text", "body": "synthetic request"},
    }


def request(event_id: str = "$one", sender: str = OWNER, room: str = ROOM) -> Request:
    req = policy().admit(room, event(event_id, sender), decrypted=True, now_ms=NOW)
    assert req is not None
    return req


def config(root: Path | str) -> dict[str, Any]:
    return dict(
        homeserver="http://127.0.0.1:18809",
        preview=True,
        account="@bot:test.invalid",
        owner="@owner:test.invalid",
        device_id="BOT",
        access_token="synthetic",
        pickle_key="x" * 32,
        rooms=["!room:test.invalid"],
        devices={"OWNER": {"ed25519": "a" * 43, "curve25519": "b" * 43}},
        state_directory=str(Path(root) / "state"),
        not_before_ms=0,
    )


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #


class TestAdmission:
    def test_only_authorized_decrypted_text_is_admitted(self) -> None:
        p = policy()
        assert p.admit(ROOM, event(), decrypted=True, now_ms=NOW) is not None
        for room, e, decrypted in [
            (ROOM, event(), False),
            ("!unknown:x", event(), True),
            (ROOM, event(sender="@intruder:example.test"), True),
            (ROOM, event(sender=BOT), True),
        ]:
            assert p.admit(room, e, decrypted=decrypted, now_ms=NOW) is None
        assert p.admit(ROOM, "not-a-dict", decrypted=True, now_ms=NOW) is None

    def test_group_requires_structured_mention_or_typed_handle(self) -> None:
        p = policy()
        local = BOT[1:].split(":")[0]
        # 이름이나 부분 문자열로는 안 된다.
        rejected = ["please respond", "Fambot please", "@" + local + "x help", "mail@" + local + ".com", "x@" + local]
        for body in rejected:
            e = event()
            e["content"]["body"] = body
            assert p.admit(GROUP, e, decrypted=True, now_ms=NOW) is None, body
        # 스펙 m.mentions는 그대로 통과.
        e = event()
        e["content"]["m.mentions"] = {"user_ids": [BOT]}
        assert p.admit(GROUP, e, decrypted=True, now_ms=NOW) is not None
        # 잘못된 m.mentions 형태는 무시된다.
        e = event()
        e["content"]["m.mentions"] = {"user_ids": "not-a-list"}
        assert p.admit(GROUP, e, decrypted=True, now_ms=NOW) is None
        # 본문에 @localpart를 통째로 치면 통과(휴대폰 앱은 pill 선택만 m.mentions를 만든다, 2026-09-17).
        typed = [
            "@" + local + " 오늘 일정 알려줘",
            "오늘 일정 @" + local.upper(),
            "(@" + local + ")",
            "@" + local + ", 안녕",
        ]
        for body in typed:
            e = event()
            e["content"]["body"] = body
            assert p.admit(GROUP, e, decrypted=True, now_ms=NOW) is not None, body
        # 직접방은 멘션 없이도 그대로.
        assert p.admit(ROOM, event(), decrypted=True, now_ms=NOW) is not None

    def test_reject_edits_plain_files_old_future_and_malformed_events(self) -> None:
        p = policy()
        cases = []
        for key, value in [
            ("type", "m.room.encrypted"),
            ("event_id", []),
            ("sender", {}),
            ("origin_server_ts", NOW - 1001),
            ("origin_server_ts", NOW + 60_001),
            ("origin_server_ts", True),
            ("content", []),
        ]:
            e = event()
            e[key] = value
            cases.append(e)
        for key, value in [
            ("msgtype", "m.file"),
            ("body", "x" * 16_385),
            ("body", "\ud800"),
            ("m.relates_to", {"rel_type": "m.replace"}),
            ("m.relates_to", []),
        ]:
            e = event()
            e["content"][key] = value
            cases.append(e)
        for e in cases:
            assert p.admit(ROOM, e, decrypted=True, now_ms=NOW) is None
        assert p.admit(ROOM, event(), decrypted=True, now_ms="now") is None

    def test_scope_separates_account_room_and_sender(self) -> None:
        scopes = {request().scope, request(sender=OTHER).scope}
        p = policy(rooms={ROOM: "direct", GROUP: "direct"})
        group = p.admit(GROUP, event(), decrypted=True, now_ms=NOW)
        assert group is not None
        scopes.add(group.scope)
        q = policy(account="@second:x", bots={"@second:x"})
        second = q.admit(ROOM, event(), decrypted=True, now_ms=NOW)
        assert second is not None
        scopes.add(second.scope)
        assert len(scopes) == 4

    def test_policy_is_frozen_and_requires_explicit_user_and_room_allowlists(self) -> None:
        rooms = {ROOM: "direct"}
        p = policy(rooms=rooms)
        rooms["!new:x"] = "direct"
        assert "!new:x" not in p.rooms
        kwargs: dict[str, Any]
        for kwargs in (
            {"users": set()},
            {"rooms": {}},
            {"users": {BOT}},
            {"bots": set()},
            {"rooms": {ROOM: "broadcast"}},
            {"not_before_ms": -1},
            {"account": "bad"},
        ):
            with pytest.raises(ValueError):
                policy(**kwargs)

    def test_bounded_text_rejects_empty_nul_and_oversize(self) -> None:
        for value in ("", "   ", "a\x00b", 5, "x" * 11):
            with pytest.raises(ValueError):
                bounded_text(value, 10)
        assert bounded_text("한글", 10) == "한글"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class TestStore:
    def test_duplicate_sync_and_pending_reply_survive_restart(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        with Store(directory, BOT) as s:
            s.accept_batch([request()], "token1")
            s.accept_batch([request()], "token2")
            job = s.claim()
            assert job is not None and job["event_id"] == "$one"
            assert s.claim() is None
            s.finish("$one", "synthetic response", "session1")
            txn = s.outbox()[0]["txn_id"]
        with Store(directory, BOT) as s:
            assert s.token() == "token2"
            assert s.outbox()[0]["txn_id"] == txn
            assert s.session(request().scope) == "session1"
            assert s.session(request(sender=OTHER).scope) is None
            s.delivered("$one")
            s.accept_batch([request()], "token3")
            assert s.claim() is None
            assert s.outbox() == []
        with pytest.raises(ValueError, match="closed"):
            s.token()

    def test_self_job_is_an_ordinary_idempotent_queued_job(self, tmp_path: Path) -> None:
        with Store(tmp_path / "state", BOT) as s:
            event = s.self_job(ROOM, OWNER, '{"kind":"x"}', key="k1")
            assert event.startswith("$self-") and len(event) == len("$self-") + 40
            assert s.self_job(ROOM, OWNER, '{"kind":"x"}', key="k1") == event  # same key+body: no-op
            with pytest.raises(SafetyStop, match="self-job-identity-conflict"):
                s.self_job(ROOM, OWNER, '{"kind":"y"}', key="k1")
            with pytest.raises(ValueError, match="invalid self-job route"):
                s.self_job("not-a-room", OWNER, "{}", key="k2")
            # Same scope as the owner's own messages: strictly serialised with them
            # in queue order (the self-job was queued first here).
            s.accept_batch([request("$msg")], "token")
            job = s.claim()
            assert job is not None and job["event_id"] == event and job["sender"] == OWNER
            assert job["scope"] == request("$msg").scope
            assert s.claim() is None  # the owner's message waits behind it
            s.finish(event, "", None)
            following = s.claim()
            assert following is not None and following["event_id"] == "$msg"
            s.finish("$msg", "", None)

    def test_crash_during_execution_is_uncertain_and_blocks_only_same_scope(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        with Store(directory, BOT) as s:
            s.accept_batch([request(), request("$two"), request("$other", OTHER)], "token")
            s.claim()
        with Store(directory, BOT) as s:
            assert len(s.uncertain()) == 1
            claimed = s.claim()
            assert claimed is not None and claimed["event_id"] == "$other"
            assert s.claim() is None
            s.resolve_uncertain("$one", "Operation result reconciled; not retried.")
            assert s.claim() is None  # Must deliver previous answer first.
            s.delivered("$one")
            claimed = s.claim()
            assert claimed is not None and claimed["event_id"] == "$two"

    def test_capacity_failure_rolls_back_whole_batch_and_token(self, tmp_path: Path) -> None:
        with Store(tmp_path / "state", BOT, total_cap=2, scope_cap=1) as s:
            s.accept_batch([], "initial")
            with pytest.raises(QueueFull):
                s.accept_batch([request(), request("$two")], "lost-token")
            assert s.token() == "initial"
            assert s.claim() is None
            s.accept_batch([request(), request("$other", OTHER)], "next")
            with pytest.raises(QueueFull):
                s.accept_batch([request("$new")], "lost-token2")
            assert s.token() == "next"

    def test_conflicting_duplicate_and_wrong_scope_fail_closed(self, tmp_path: Path) -> None:
        with Store(tmp_path / "state", BOT) as s:
            s.accept_batch([request()], "first")
            bad = Request("$one", ROOM, OWNER, "changed body", request().scope)
            with pytest.raises(ValueError):
                s.accept_batch([bad], "bad-token")
            bad = Request("$two", ROOM, OWNER, "text", request(sender=OTHER).scope)
            with pytest.raises(ValueError):
                s.accept_batch([bad], "bad-token")
            with pytest.raises(ValueError):
                s.accept_batch([Request("no-dollar", ROOM, OWNER, "text", SCOPE)], "bad-token")
            with pytest.raises(ValueError):
                s.accept_batch(["not a request"], "bad-token")
            assert s.token() == "first"

    def test_identity_pin_and_invalid_state_transitions(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        with Store(directory, BOT) as s:
            s.accept_batch([request()], "first")
            with pytest.raises(ValueError):
                s.finish("$one", "answer")
            with pytest.raises(ValueError):
                s.delivered("$one")
            with pytest.raises(ValueError):
                s.resolve_uncertain("$one", "answer")
        with pytest.raises(ValueError):
            Store(directory, "@different:x")
        with pytest.raises(ValueError):
            Store(directory, BOT, total_cap=0)
        with Store(directory, BOT) as s:
            assert s.token() == "first"

    def test_empty_reply_finishes_without_delivery(self, tmp_path: Path) -> None:
        with Store(tmp_path / "state", BOT) as s:
            s.accept_batch([request(), request("$two")], "t")
            s.claim()
            s.finish("$one", "", "session-a")
            assert s.outbox() == []
            assert s.session(request().scope) == "session-a"
            claimed = s.claim()
            assert claimed is not None and claimed["event_id"] == "$two"  # nothing pending in the scope
            with pytest.raises(ValueError):
                s.finish("$two", None)
            with pytest.raises(ValueError):
                s.finish("$two", "x" * (MAX_REPLY_BYTES + 1))
            s.finish("$two", "   ")  # whitespace-only counts as nothing to say
            assert s.outbox() == []

    def test_second_process_cannot_open_same_store(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        with Store(directory, BOT):
            code = "import sys;from telegram_bot.core.matrix.state import Store;Store(sys.argv[1],sys.argv[2])"
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
            p = subprocess.run(
                [sys.executable, "-c", code, str(directory), BOT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                env=env,
            )
            assert p.returncode != 0

    def test_files_private_under_permissive_umask(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        old = os.umask(0o022)
        try:
            with Store(directory, BOT) as s:
                s.accept_batch([request()], "token")
        finally:
            os.umask(old)
        assert directory.stat().st_mode & 0o777 == 0o700
        for p in directory.iterdir():
            assert p.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Linux path descriptors")
    def test_traversable_unreadable_ancestors_and_readable_leaf(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        real_open = os.open

        def android_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            # Model Android's EACCES for directory reads outside the app root.
            if flags & os.O_DIRECTORY and str(path) != "state" and not flags & os.O_PATH:
                raise PermissionError("ancestor permits traversal only")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(m.os, "open", side_effect=android_open):
            fd = m.private_directory(directory)
            try:
                assert os.listdir(fd) == []  # O_PATH must never escape as the leaf fd.
                child = m.private_file(fd, "probe")
                os.close(child)
                assert os.listdir(fd) == ["probe"]
            finally:
                os.close(fd)

    def test_private_directory_without_path_descriptors(self, tmp_path: Path) -> None:
        with patch.object(m.os, "O_PATH", os.O_RDONLY, create=True):
            fd = m.private_directory(tmp_path / "state")
            try:
                assert os.listdir(fd) == []
            finally:
                os.close(fd)

    def test_symlink_ancestor_directory_database_lock_and_journal_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        for path in [alias, alias / "state"]:
            with pytest.raises(OSError):
                Store(path, BOT)
        (tmp_path / "state").mkdir(mode=0o700)
        target = real / "sentinel"
        target.write_text("do not alter")
        for name in ["inbox.lock", "inbox.sqlite3", "inbox.sqlite3-journal"]:
            # Use isolated store directories so test-created links need not be removed.
            sub = real / name.replace(".", "_")
            sub.mkdir(mode=0o700)
            (sub / name).symlink_to(target)
            with pytest.raises(OSError):
                Store(sub, BOT)
        assert target.read_text() == "do not alter"
        with pytest.raises(ValueError):
            Store("relative/path", BOT)
        with pytest.raises(ValueError):
            Store(tmp_path / ".." / "x", BOT)

    def test_hardlinks_and_world_accessible_directory_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with pytest.raises(ValueError):
            Store(directory, BOT)
        directory.chmod(0o700)
        target = tmp_path / "sentinel"
        target.write_text("unchanged")
        os.link(target, directory / "inbox.sqlite3")
        with pytest.raises(ValueError):
            Store(directory, BOT)
        assert target.read_text() == "unchanged"

    def test_failed_open_does_not_leak_fds(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        directory.mkdir(mode=0o700)
        (directory / "inbox.lock").symlink_to("/dev/null")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            with pytest.raises(OSError):
                Store(directory, BOT)
        assert len(os.listdir("/proc/self/fd")) == before


# --------------------------------------------------------------------------- #
# MatrixStore: sync replay, notices, controls, operator block
# --------------------------------------------------------------------------- #


def uncertain_job(store: MatrixStore, event_id: str = "$job", sender: str = OWNER) -> None:
    scope = hashlib.sha256(json.dumps([BOT, ROOM, sender]).encode()).hexdigest()
    store.accept_batch([Request(event_id, ROOM, sender, "synthetic prompt", scope)], "token")
    store.claim()
    store.uncertain_job(event_id)


class TestMatrixStore:
    def test_raw_pending_survives_reopen_and_atomic_commit(self, tmp_path: Path) -> None:
        with MatrixStore(tmp_path / "state", BOT) as store:
            store.accept_batch([], "old")
            store.stage_sync({"next_batch": "new", "to_device": {"events": [{"ciphertext": "synthetic"}]}})
            with pytest.raises(SafetyStop, match="pending-sync-not-finished"):
                store.stage_sync({"next_batch": "newer"})
        with MatrixStore(tmp_path / "state", BOT) as store:
            assert store.token() == "old"
            assert store.get_meta("pending_sync")["next_batch"] == "new"
            with pytest.raises(SafetyStop, match="sync-identity-mismatch"):
                store.commit_sync("wrong")
            store.commit_sync("new")
            assert store.get_meta("pending_sync") is None
            assert store.token() == "new"
            with pytest.raises(SafetyStop, match="sync-identity-mismatch"):
                store.commit_sync("new")
            with pytest.raises(SafetyStop, match="invalid-sync"):
                store.stage_sync(["not", "a", "dict"])
            with pytest.raises(ValueError):
                store.stage_sync({"no": "token"})
            with pytest.raises(SafetyStop, match="sync-too-large"):
                store.stage_sync({"next_batch": "big", "blob": "x" * 4_194_304})

    def test_unicode_chunks_are_lossless_and_bounded(self) -> None:
        text = "한글🙂" * 10000
        chunks = parts(text)
        assert "".join(chunks) == text
        assert all(len(p.encode()) <= 12000 for p in chunks)
        assert len(chunks) > 1
        assert parts("") == []

    def test_storage_guard(self, tmp_path: Path) -> None:
        with MatrixStore(tmp_path / "state", BOT) as s:
            s.storage_gate()
            with patch.object(m.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=1, f_frsize=4096)):
                with pytest.raises(SafetyStop, match="pilot-storage-limit"):
                    s.storage_gate()

    def test_notice_dedup_and_identity_conflict(self, tmp_path: Path) -> None:
        with MatrixStore(tmp_path / "state", BOT) as s:
            req = request()
            first = s.notice(req, "k", "안내")
            assert s.notice(req, "k", "안내") == first
            assert len(s.outbox()) == 1
            with pytest.raises(SafetyStop, match="notice-identity-conflict"):
                s.notice(req, "k", "다른 안내")
            assert s.delivered_parts(first) == 0
            s.mark_part(first, 2)
            assert s.delivered_parts(first) == 2

    def test_control_dedup_records_only_when_asked(self, tmp_path: Path) -> None:
        with MatrixStore(tmp_path / "state", BOT) as s:
            req = Request("$c", ROOM, OWNER, "/ack x", SCOPE)
            assert s.seen_control(req, record=False) is False
            assert s.seen_control(req) is False
            assert s.seen_control(req) is True
            with pytest.raises(SafetyStop, match="control-identity-conflict"):
                s.seen_control(Request("$c", ROOM, OWNER, "/ack y", SCOPE))

    def test_uncertain_scopes_block_and_operator_unblock_is_audited(self, tmp_path: Path) -> None:
        directory = tmp_path / "state"
        with MatrixStore(directory, BOT) as s:
            assert s.block() is None
            with pytest.raises(ValueError, match="no-block"):
                s.unblock(SCOPE, "reason", operator_name())
            uncertain_job(s)
            uncertain_job(s, "$other-job", OTHER)
            block = s.block()
            assert block is not None
            assert set(BLOCK_KEYS) <= set(block)
            assert SCOPE in block["blocked_scopes"] and len(block["blocked_scopes"]) == 2
            assert block["uncertain_turns"][0] == {"turn_id": turn_id("$job"), "scope": SCOPE, "room_id": ROOM}
            assert "synthetic prompt" not in json.dumps(block)
            result = s.unblock(SCOPE, "runtime pid verified exited", operator_name())
            assert result["cleared"] == ["$job"]
            assert result["audit_seq"] == 1
            assert [j["event_id"] for j in s.uncertain()] == ["$other-job"]  # other scope untouched
            assert [j["reply"] for j in s.outbox()] == [OPERATOR_ACK_TEXT]
            with pytest.raises(ValueError, match="scope-not-blocked"):
                s.unblock(SCOPE, "again", operator_name())
        with MatrixStore(directory, BOT) as s:
            audit = s.audit()
            assert len(audit) == 1
            assert (audit[0]["action"], audit[0]["scope"], audit[0]["reason"]) == (
                "unblock",
                SCOPE,
                "runtime pid verified exited",
            )
            assert audit[0]["actor"] == operator_name()
            assert json.loads(audit[0]["before"])["blocked_scopes"] == sorted(block["blocked_scopes"])
            assert "synthetic prompt" not in audit[0]["before"]

    def test_other_scope_or_invalid_input_changes_nothing(self, tmp_path: Path) -> None:
        with MatrixStore(tmp_path / "state", BOT) as s:
            uncertain_job(s)
            for scope, reason in ((FOREIGN_SCOPE, "ok"), ("not-hex", "ok"), (SCOPE, ""), (SCOPE, "x" * 2000)):
                with pytest.raises(ValueError):
                    s.unblock(scope, reason, operator_name())
            with pytest.raises(ValueError):
                s.unblock(SCOPE, "ok", "")
            block = s.block()
            assert block is not None and block["blocked_scopes"] == [SCOPE]
            assert s.audit() == []
            assert len(s.uncertain()) == 1

    def test_operator_name_falls_back_to_uid(self) -> None:
        assert operator_name({"SUDO_USER": "alice"}) == "alice#" + str(os.getuid())
        with patch.object(m.getpass, "getuser", side_effect=OSError):
            assert operator_name({}) == "uid" + str(os.getuid()) + "#" + str(os.getuid())


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class TestConfig:
    def test_config_permissions_and_symlinks(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config(tmp_path)))
        path.chmod(0o600)
        tmp_path.chmod(0o700)
        assert load_config(path)["preview"] is True
        path.chmod(0o644)
        with pytest.raises(SafetyStop, match="private-config-required"):
            load_config(path)
        link = tmp_path / "link"
        link.symlink_to(path)
        with pytest.raises(OSError):
            load_config(link)

    def test_config_disallows_remote_http_human_device_and_bad_shapes(self, tmp_path: Path) -> None:
        c = config(tmp_path)
        assert validate_config(dict(c)) == c
        assert validate_config({**c, "homeserver": "https://matrix.example.invalid"})
        update: dict[str, Any]
        for update in (
            {"homeserver": "http://example.invalid"},
            {"homeserver": "https://user:pw@example.invalid"},
            {"homeserver": "https://example.invalid/path"},
            {"homeserver": 7},
            {"account": c["owner"]},
            {"account": "owner"},
            {"owner": 5},
            {"device_id": "bad device"},
            {"access_token": ""},
            {"pickle_key": "short"},
            {"devices": {}},
            {"devices": {"OWNER": "not-a-dict"}},
            {"devices": {"bad device": {"ed25519": "a" * 43, "curve25519": "b" * 43}}},
            {"devices": {"OWNER": {"ed25519": "a" * 42, "curve25519": "b" * 43}}},
            {"state_directory": "relative"},
            {"state_directory": "/abs/../up"},
            {"state_directory": 5},
            {"rooms": []},
            {"rooms": ["not-a-room"]},
            {"not_before_ms": -1},
        ):
            with pytest.raises((SafetyStop, ValueError)):
                validate_config({**c, **update})
        with pytest.raises(SafetyStop, match="invalid-config"):
            validate_config("nope")
        with pytest.raises(SafetyStop, match="missing-config-fields"):
            validate_config({k: v for k, v in c.items() if k != "owner"})
        # Pilot-era worker keys are ignored rather than required.
        assert validate_config({**c, "worker_argv": ["relative"], "remote_worker": "yes"})

    def test_family_config_validation(self, tmp_path: Path) -> None:
        family = "!family:test.invalid"
        dad, mom, stranger = "@dad:test.invalid", "@mom:test.invalid", "@stranger:test.invalid"
        keyset = {"ed25519": "c" * 43, "curve25519": "c" * 43}
        c = config(tmp_path)
        base = {
            **c,
            "rooms": [c["rooms"][0], family],
            "family_rooms": [family],
            "family_users": [dad, mom],
            "family_devices": {dad: {"DAD1": keyset}},
        }
        rooms, users, devices = family_config(base)
        assert (rooms, users) == (frozenset([family]), frozenset([dad, mom]))
        assert devices == {dad: {"DAD1": keyset}}
        assert family_config(c) == (frozenset(), frozenset(), {})
        update: dict[str, Any]
        for update, reason in (
            ({"family_rooms": ["!elsewhere:test.invalid"]}, "invalid-family-rooms"),
            ({"family_rooms": "no"}, "invalid-family_rooms"),
            ({"family_users": [base["account"]]}, "invalid-family-users"),
            ({"family_users": ["표시이름"]}, "invalid-family-users"),
            ({"family_users": [dad, dad]}, "invalid-family_users"),
            ({"family_rooms": [family], "family_users": []}, "invalid-family-users"),
            ({"family_devices": {mom: {}}}, "invalid-family-devices"),
            ({"family_devices": {stranger: {"S1": keyset}}}, "invalid-family-devices"),
            ({"family_devices": {c["owner"]: {"O1": keyset}}}, "invalid-family-devices"),
            ({"family_devices": []}, "invalid-family-devices"),
            ({"family_devices": {dad: {"DAD1": {"ed25519": "c" * 42, "curve25519": "d" * 43}}}}, "invalid-family-device-pin"),
            ({"family_devices": {dad: {"bad device": keyset}}}, "invalid-family-device-pin"),
            ({"family_devices": {dad: {f"D{i}": keyset for i in range(11)}}}, "invalid-family-devices"),
            ({"family_notice_text": 123}, "invalid-family-notice-text"),
        ):
            with pytest.raises(SafetyStop, match=reason):
                family_config({**base, **update})
        # More than 32 pinned family devices in total is refused.
        kids = ["@kid1:test.invalid", "@kid2:test.invalid"]
        many = {u: {f"D{i}": keyset for i in range(10)} for u in (dad, mom, *kids)}
        with pytest.raises(SafetyStop, match="invalid-family-devices"):
            family_config({**base, "family_users": [dad, mom, *kids], "family_devices": many})
        assert family_config({**base, "family_notice_text": "안내"})[0] == frozenset([family])

    def test_saved_policy_upgrade_drops_worker_keys(self, tmp_path: Path) -> None:
        c = config(tmp_path)
        current = saved_policy(c)
        assert set(current) == {"owner", "rooms", "devices", "not_before_ms", "family_rooms", "family_users", "family_devices", "identities"}
        pilot = {k: c[k] for k in ("owner", "rooms", "devices", "not_before_ms")}
        pilot["worker_argv"] = ["/usr/bin/python3", "/opt/worker.py"]
        pilot["worker_argv_family"] = None
        pilot["remote_worker"] = True
        assert upgrade_saved_policy(pilot) == current
        assert upgrade_saved_policy({**pilot, "owner": "@changed:test.invalid"}) != current

    def test_turn_id_is_stable_and_short(self) -> None:
        assert turn_id("$one") == turn_id("$one")
        assert re.fullmatch(r"[0-9a-f]{32}", turn_id("$one"))
        assert turn_id("$one") != turn_id("$two")


def test_every_raised_safety_stop_reason_is_listed() -> None:
    root = Path(m.__file__).parent
    raised = set()
    for source in (root / "state.py", root / "transport.py"):
        raised |= set(re.findall(r'SafetyStop\(\s*"([a-z0-9_-]+)"\s*\)', source.read_text()))
    assert raised <= SAFETY_STOP_REASONS, raised - SAFETY_STOP_REASONS
    # `+ key` / `+ str(status)` forms use a listed prefix.
    assert {"invalid-family_rooms", "invalid-family_users", "matrix-http-"} <= SAFETY_STOP_REASONS
    for legacy in ("worker-cleanup-unconfirmed", "invalid-worker-command", "invalid-remote-mode"):
        assert legacy not in SAFETY_STOP_REASONS


def test_mention_aliases_widen_the_typed_handle_gate_only() -> None:
    # Matrix ids cannot be renamed: the bot stays @bot but the family calls it "@seoseo".
    p = policy(aliases={"seoseo"})
    for body, expected in [
        ("@seoseo 오늘 일정", True),
        ("(@SEOSEO)", True),
        ("@seoseox 안녕", False),
        ("mail@seoseo.com", False),
        ("seoseo 안녕", False),
        ("@" + BOT[1:].split(":")[0] + " 안녕", True),
    ]:
        e = event()
        e["content"]["body"] = body
        assert (p.admit(GROUP, e, decrypted=True, now_ms=NOW) is not None) is expected, body
    # Direct rooms never needed a mention and still do not.
    e = event()
    e["content"]["body"] = "그냥 질문"
    assert p.admit(ROOM, e, decrypted=True, now_ms=NOW) is not None
    with pytest.raises(ValueError):
        policy(aliases={"Bad Alias"})


def test_mention_aliases_config_validation() -> None:
    assert mention_aliases({}) == frozenset()
    assert mention_aliases({"mention_aliases": ["seoseo", "bot-2"]}) == {"seoseo", "bot-2"}
    for bad in ("seoseo", ["Seoseo"], ["a b"], [""], ["x"] * 9, ["dup", "dup"], [1]):
        with pytest.raises(SafetyStop, match="invalid-mention-aliases"):
            mention_aliases({"mention_aliases": bad})


def test_wake_words_answer_bare_nicknames() -> None:
    # Korean nicknames have no @handle: the family calls the bot "서서" / "서서야".
    p = policy(wake_words={"서서", "서서야"})
    for body, expected in [
        ("서서야 오늘 일정", True),
        ("서서 오늘 일정", True),
        ("서서, 그건 좀 아니지", True),
        ("야 서서!", True),
        ("서서", True),
        ("서서야뭐해", True),  # no-space typing: wake word + particle + rest
        ("서서는이제그만", True),
        ("서서랑놀자", True),
        ("서서님안녕", True),
        ("서서를봐줘", True),
        ("서서에게말해", True),
        ("오늘 서서히 풀리네", False),  # "서서" glued inside another word
        ("서서울가자", False),  # 울 is not a particle
        ("우리서서 별로야", False),
        ("@서서야 안녕", True),  # a typed @handle still counts
    ]:
        e = event()
        e["content"]["body"] = body
        assert (p.admit(GROUP, e, decrypted=True, now_ms=NOW) is not None) is expected, body
    # Direct rooms remain unconditioned.
    e = event()
    e["content"]["body"] = "멘션 없는 대화"
    assert p.admit(ROOM, e, decrypted=True, now_ms=NOW) is not None
    with pytest.raises(ValueError):
        policy(wake_words={"Bad Word"})


def test_turn_timeout_minutes_config_validation() -> None:
    assert turn_timeout_minutes({}) == 360.0
    assert turn_timeout_minutes({"turn_timeout_minutes": 360}) == 360.0
    assert turn_timeout_minutes({"turn_timeout_minutes": 7.5}) == 7.5
    for bad in (True, 4, 361, "x", None):
        with pytest.raises(SafetyStop, match="invalid-turn-timeout"):
            turn_timeout_minutes({"turn_timeout_minutes": bad})


def test_wake_words_config_validation() -> None:
    assert wake_words({}) == frozenset()
    assert wake_words({"wake_words": ["서서", "서서야"]}) == {"서서", "서서야"}
    for bad in ("서서", ["Seo seo"], ["서서-야"], [""], ["x"] * 9, ["dup", "dup"], [1]):
        with pytest.raises(SafetyStop, match="invalid-wake-words"):
            wake_words({"wake_words": bad})


def test_aliases_and_wake_words_stack() -> None:
    p = policy(aliases={"seoseo"}, wake_words={"서서"})
    for body in ("@seoseo 안녕", "서서 안녕"):
        e = event()
        e["content"]["body"] = body
        assert p.admit(GROUP, e, decrypted=True, now_ms=NOW) is not None, body


def test_identities_replace_owner_pins_and_join_the_saved_policy(tmp_path: Path) -> None:
    c = config(tmp_path)
    master = "M" * 43
    with_identity = {**c, "devices": {}, "identities": {c["owner"]: {"master": master}}}
    assert identities(with_identity) == {c["owner"]: master}
    validate_config(dict(with_identity))  # empty device pins are fine once the owner has an identity
    with pytest.raises(SafetyStop, match="pin-owner-devices"):
        validate_config({**c, "devices": {}})  # no identity: pins still required
    policy = saved_policy(with_identity)
    assert policy["identities"] == {c["owner"]: master} and policy["devices"] == {}
    old = {k: v for k, v in policy.items() if k != "identities"}
    assert upgrade_saved_policy(old) == {**policy, "identities": {}}  # pre-#149 policies upgrade to "no identities"
    for bad in ({"@stranger:test.invalid": {"master": master}}, {c["owner"]: {"master": "short"}},
                {c["owner"]: "M" * 43}, "not-a-dict", {c["owner"]: {}}):
        with pytest.raises(SafetyStop, match="invalid-identities"):
            identities({**c, "identities": bad})
    family = {**c, "family_rooms": [c["rooms"][0]], "family_users": [OWNER2 := "@dad:test.invalid"],
              "identities": {OWNER2: {"master": "D" * 43}}}
    assert identities(family) == {OWNER2: "D" * 43}
