"""Deterministic coverage for the webhook nudge listener (#1222).

No network beyond loopback sockets, no provider calls, no ``gh`` execution:
the listener's whole contract is that it may only pull ``next_poll_epoch``
forward on already-registered waits, so every test asserts registry effects
(or their absence) directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from telegram_bot.core.external_wait import ExternalWaitRegistry
from telegram_bot.core.webhook_nudge import (
    NUDGE_PATH,
    NUDGE_POLL_INTERVAL_SECONDS,
    NudgeTarget,
    WebhookNudgeServer,
    apply_nudge,
    build_from_env,
    parse_nudge_targets,
    verify_signature,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


SECRET = "test-secret"
REPO = "jinwon-int/ccc-node"
HEAD = "a" * 40


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_registry(tmp_path: Path, clock: Clock) -> Tuple[ExternalWaitRegistry, str]:
    registry = ExternalWaitRegistry(tmp_path / "registry.json", clock=clock)
    wait_id = registry.register(
        repo=REPO,
        pr_number=42,
        head_sha=HEAD,
        user_id=7,
        chat_id=7,
        session_id="s1",
        summary="ci wait",
        timeout_seconds=3600,
        poll_interval_seconds=30,
        now=clock.now,
    )
    # Simulate a deep backoff: the next poll sits 300s out.
    registry.reschedule(wait_id, next_poll_epoch=clock.now + 300, poll_interval_seconds=300)
    return registry, wait_id


def record_of(registry: ExternalWaitRegistry, wait_id: str) -> Dict:
    record = registry.get(wait_id)
    assert record is not None
    return record


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def workflow_run_payload(
    *, repo: str = REPO, head_sha: str = HEAD, pr_numbers=(42,)
) -> Dict:
    return {
        "action": "completed",
        "repository": {"full_name": repo},
        "workflow_run": {
            "head_sha": head_sha,
            "pull_requests": [{"number": n} for n in pr_numbers],
        },
    }


# -- signature ---------------------------------------------------------------------


def test_verify_signature_accepts_valid_header() -> None:
    body = b'{"x":1}'
    assert verify_signature(SECRET, body, sign(body))


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=deadbeef",
        "sha1=deadbeef",
        "not-a-signature",
    ],
)
def test_verify_signature_rejects_bad_headers(header: Optional[str]) -> None:
    assert not verify_signature(SECRET, b"{}", header)


def test_verify_signature_rejects_wrong_secret_and_empty_secret() -> None:
    body = b"{}"
    assert not verify_signature(SECRET, body, sign(body, "other-secret"))
    assert not verify_signature("", body, sign(body, ""))


# -- payload parsing ---------------------------------------------------------------


def test_parse_workflow_run_extracts_repo_prs_and_head() -> None:
    targets = parse_nudge_targets("workflow_run", workflow_run_payload())
    assert targets == [NudgeTarget(repo=REPO, pr_numbers=(42,), head_sha=HEAD)]


def test_parse_check_suite_extracts_head() -> None:
    payload = {
        "repository": {"full_name": REPO},
        "check_suite": {"head_sha": HEAD.upper(), "pull_requests": []},
    }
    targets = parse_nudge_targets("check_suite", payload)
    assert targets == [NudgeTarget(repo=REPO, pr_numbers=(), head_sha=HEAD)]


def test_parse_pull_request_extracts_number_and_head() -> None:
    payload = {
        "repository": {"full_name": REPO},
        "pull_request": {"number": 42, "head": {"sha": HEAD}},
    }
    targets = parse_nudge_targets("pull_request", payload)
    assert targets == [NudgeTarget(repo=REPO, pr_numbers=(42,), head_sha=HEAD)]


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("push", {"repository": {"full_name": REPO}}),
        ("workflow_run", {}),
        ("workflow_run", {"repository": {"full_name": "not-a-repo"}}),
        ("workflow_run", {"repository": {"full_name": REPO}, "workflow_run": "x"}),
        (
            "workflow_run",
            {"repository": {"full_name": REPO}, "workflow_run": {"pull_requests": []}},
        ),
        ("pull_request", {"repository": {"full_name": REPO}, "pull_request": {}}),
        (
            "pull_request",
            {
                "repository": {"full_name": REPO},
                "pull_request": {"number": "x", "head": {"sha": HEAD}},
            },
        ),
    ],
)
def test_parse_rejects_unknown_or_malformed(event: str, payload: Dict) -> None:
    assert parse_nudge_targets(event, payload) == []


def test_parse_tolerates_malformed_pr_entries() -> None:
    payload = workflow_run_payload()
    payload["workflow_run"]["pull_requests"] = [
        {"number": "not-a-number"},
        "junk",
        {"number": 42},
    ]
    targets = parse_nudge_targets("workflow_run", payload)
    assert targets[0].pr_numbers == (42,)


# -- nudge application -------------------------------------------------------------


def test_nudge_by_pr_number_pulls_poll_forward_and_resets_backoff(
    tmp_path: Path,
) -> None:
    clock = Clock()
    registry, wait_id = make_registry(tmp_path, clock)
    count = apply_nudge(
        registry,
        [NudgeTarget(repo=REPO.upper(), pr_numbers=(42,), head_sha="")],
        clock=clock,
    )
    assert count == 1
    record = record_of(registry, wait_id)
    assert record["next_poll_epoch"] == clock.now
    assert record["poll_interval_seconds"] == NUDGE_POLL_INTERVAL_SECONDS


def test_nudge_by_head_sha_matches_short_recorded_prefix(tmp_path: Path) -> None:
    clock = Clock()
    registry = ExternalWaitRegistry(tmp_path / "registry.json", clock=clock)
    wait_id = registry.register(
        repo=REPO,
        pr_number=42,
        head_sha=HEAD[:12],  # legacy short registration (#961)
        user_id=7,
        chat_id=7,
        session_id=None,
        summary=None,
        timeout_seconds=3600,
        poll_interval_seconds=30,
        now=clock.now,
    )
    registry.reschedule(wait_id, next_poll_epoch=clock.now + 300, poll_interval_seconds=300)
    count = apply_nudge(
        registry,
        [NudgeTarget(repo=REPO, pr_numbers=(), head_sha=HEAD)],
        clock=clock,
    )
    assert count == 1


def test_nudge_ignores_other_repo_and_pr(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id = make_registry(tmp_path, clock)
    before = record_of(registry, wait_id)["next_poll_epoch"]
    assert (
        apply_nudge(
            registry,
            [NudgeTarget(repo="other/repo", pr_numbers=(42,), head_sha=HEAD)],
            clock=clock,
        )
        == 0
    )
    assert (
        apply_nudge(
            registry,
            [NudgeTarget(repo=REPO, pr_numbers=(999,), head_sha="b" * 40)],
            clock=clock,
        )
        == 0
    )
    assert record_of(registry, wait_id)["next_poll_epoch"] == before


def test_nudge_is_idempotent_once_due(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id = make_registry(tmp_path, clock)
    target = [NudgeTarget(repo=REPO, pr_numbers=(42,), head_sha="")]
    assert apply_nudge(registry, target, clock=clock) == 1
    # Repeating the same delivery finds the wait already due: no-op.
    for _ in range(10):
        assert apply_nudge(registry, target, clock=clock) == 0


def test_nudge_skips_finished_waits(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id = make_registry(tmp_path, clock)
    registry.finish(wait_id, "success", now=clock.now)
    assert (
        apply_nudge(
            registry,
            [NudgeTarget(repo=REPO, pr_numbers=(42,), head_sha=HEAD)],
            clock=clock,
        )
        == 0
    )


def test_nudge_with_no_targets_is_a_noop(tmp_path: Path) -> None:
    clock = Clock()
    registry, _ = make_registry(tmp_path, clock)
    assert apply_nudge(registry, [], clock=clock) == 0


# -- HTTP listener ------------------------------------------------------------------


async def start_server(
    tmp_path: Path, clock: Clock, **kwargs
) -> Tuple[WebhookNudgeServer, ExternalWaitRegistry, str]:
    registry, wait_id = make_registry(tmp_path, clock)
    server = WebhookNudgeServer(
        registry, secret=SECRET, host="127.0.0.1", port=0, clock=clock, **kwargs
    )
    assert await server.start()
    return server, registry, wait_id


async def send_raw(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    return response


async def send_request(
    port: int,
    body: bytes,
    *,
    method: str = "POST",
    path: str = NUDGE_PATH,
    event: str = "workflow_run",
    signature: Optional[str] = None,
    content_length: Optional[int] = None,
) -> bytes:
    length = len(body) if content_length is None else content_length
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: 127.0.0.1",
        f"Content-Length: {length}",
        f"X-GitHub-Event: {event}",
    ]
    if signature is not None:
        headers.append(f"X-Hub-Signature-256: {signature}")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode() + body
    return await send_raw(port, raw)


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


@pytest.mark.anyio
async def test_valid_delivery_nudges_and_returns_204(tmp_path: Path) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(tmp_path, clock)
    try:
        body = json.dumps(workflow_run_payload()).encode()
        response = await send_request(server.port, body, signature=sign(body))
        assert status_of(response) == 204
        assert record_of(registry, wait_id)["next_poll_epoch"] == clock.now
    finally:
        await server.close()


@pytest.mark.anyio
async def test_bad_signature_is_rejected_without_registry_access(
    tmp_path: Path,
) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(tmp_path, clock)
    try:
        before = record_of(registry, wait_id)["next_poll_epoch"]
        body = json.dumps(workflow_run_payload()).encode()
        response = await send_request(
            server.port, body, signature=sign(body, "wrong-secret")
        )
        assert status_of(response) == 401
        response = await send_request(server.port, body, signature=None)
        assert status_of(response) == 401
        assert record_of(registry, wait_id)["next_poll_epoch"] == before
    finally:
        await server.close()


@pytest.mark.anyio
async def test_non_matching_delivery_returns_204_and_changes_nothing(
    tmp_path: Path,
) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(tmp_path, clock)
    try:
        before = record_of(registry, wait_id)["next_poll_epoch"]
        body = json.dumps(
            workflow_run_payload(repo="other/repo", head_sha="b" * 40, pr_numbers=(9,))
        ).encode()
        response = await send_request(server.port, body, signature=sign(body))
        assert status_of(response) == 204
        assert record_of(registry, wait_id)["next_poll_epoch"] == before
    finally:
        await server.close()


@pytest.mark.anyio
async def test_http_surface_rejections(tmp_path: Path) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(tmp_path, clock)
    try:
        body = json.dumps(workflow_run_payload()).encode()
        signature = sign(body)

        response = await send_request(
            server.port, body, path="/other", signature=signature
        )
        assert status_of(response) == 404

        response = await send_request(
            server.port, body, method="GET", signature=signature
        )
        assert status_of(response) == 405

        raw = (
            f"POST {NUDGE_PATH} HTTP/1.1\r\nHost: x\r\n"
            f"X-Hub-Signature-256: {signature}\r\n\r\n"
        ).encode()
        assert status_of(await send_raw(server.port, raw)) == 411

        response = await send_request(
            server.port, body, signature=signature, content_length=64 * 1024 * 1024
        )
        assert status_of(response) == 413

        bad = b"not json"
        response = await send_request(server.port, bad, signature=sign(bad))
        assert status_of(response) == 400

        # Nothing above may touch the registry.
        assert record_of(registry, wait_id)["next_poll_epoch"] == clock.now + 300
    finally:
        await server.close()


@pytest.mark.anyio
async def test_repeated_delivery_is_idempotent_over_http(tmp_path: Path) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(tmp_path, clock)
    try:
        body = json.dumps(workflow_run_payload()).encode()
        for _ in range(3):
            response = await send_request(server.port, body, signature=sign(body))
            assert status_of(response) == 204
        record = record_of(registry, wait_id)
        assert record["next_poll_epoch"] == clock.now
        assert record["state"] == "monitoring"
    finally:
        await server.close()


@pytest.mark.anyio
async def test_rate_limit_returns_429(tmp_path: Path) -> None:
    clock = Clock()
    server, registry, _ = await start_server(
        tmp_path, clock, rate_limit_per_minute=2
    )
    try:
        body = json.dumps(workflow_run_payload()).encode()
        signature = sign(body)
        assert status_of(await send_request(server.port, body, signature=signature)) == 204
        assert status_of(await send_request(server.port, body, signature=signature)) == 204
        assert status_of(await send_request(server.port, body, signature=signature)) == 429
        clock.now += 61  # a fresh minute window opens again
        assert status_of(await send_request(server.port, body, signature=signature)) == 204
    finally:
        await server.close()


@pytest.mark.anyio
async def test_bind_failure_degrades_without_raising(tmp_path: Path) -> None:
    clock = Clock()
    server, _, _ = await start_server(tmp_path, clock)
    try:
        registry = ExternalWaitRegistry(tmp_path / "other.json", clock=clock)
        rival = WebhookNudgeServer(
            registry, secret=SECRET, host="127.0.0.1", port=server.port, clock=clock
        )
        assert not await rival.start()
        await rival.close()  # close without a live server must be a no-op
    finally:
        await server.close()


# -- accept-loop resilience (#1274) --------------------------------------------------
#
# A dead-interface socket makes every accept() fail with the same OSError
# forever. These tests drive that failure pattern directly (by monkeypatching
# the running loop's ``sock_accept``) rather than actually downing a network
# interface, and assert the listener backs off, self-heals below its failure
# threshold, rebinds once the threshold is crossed, and gives up cleanly
# (without crashing or leaking tasks) if rebinding itself never recovers.


@pytest.mark.anyio
async def test_accept_failures_below_threshold_recover_without_rebind(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(tmp_path, clock)
    loop = asyncio.get_running_loop()
    real_accept = loop.sock_accept
    calls = {"n": 0}

    async def flaky(sock):
        calls["n"] += 1
        if calls["n"] <= 4:
            raise OSError(22, "Invalid argument")
        return await real_accept(sock)

    rebind_calls = {"n": 0}
    original_bind = server._bind_socket

    def counting_bind():
        rebind_calls["n"] += 1
        return original_bind()

    monkeypatch.setattr(loop, "sock_accept", flaky)
    monkeypatch.setattr(server, "_bind_socket", counting_bind)
    try:
        body = json.dumps(workflow_run_payload()).encode()
        response = await send_request(server.port, body, signature=sign(body))
        assert status_of(response) == 204
        assert calls["n"] >= 5
        # Four failures never reach the (default 20) threshold: the loop
        # backs off and keeps retrying the *same* socket, no rebind needed.
        assert rebind_calls["n"] == 0
    finally:
        await server.close()


@pytest.mark.anyio
async def test_accept_failures_at_threshold_trigger_rebind(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    server, registry, wait_id = await start_server(
        tmp_path, clock, max_consecutive_accept_failures=3
    )
    loop = asyncio.get_running_loop()

    async def always_fail(sock):
        raise OSError(22, "Invalid argument")

    rebind_calls = {"n": 0}
    original_bind = server._bind_socket

    def counting_bind():
        rebind_calls["n"] += 1
        return original_bind()

    monkeypatch.setattr(loop, "sock_accept", always_fail)
    monkeypatch.setattr(server, "_bind_socket", counting_bind)
    try:
        for _ in range(50):
            if rebind_calls["n"] >= 1:
                break
            await asyncio.sleep(0.05)
        assert rebind_calls["n"] >= 1

        # Restore the real accept/bind path and confirm the *rebound*
        # socket actually serves a fresh request (proves the old dead
        # socket was replaced, not just closed).
        monkeypatch.undo()
        body = json.dumps(workflow_run_payload()).encode()
        response = await send_request(server.port, body, signature=sign(body))
        assert status_of(response) == 204
    finally:
        await server.close()


@pytest.mark.anyio
async def test_rebind_gives_up_after_max_attempts_and_stays_closable(
    tmp_path: Path, monkeypatch
) -> None:
    import telegram_bot.core.webhook_nudge as webhook_nudge_module

    # Speed the retry cadence up for the test; production keeps the real
    # 30s pacing between rebind attempts.
    monkeypatch.setattr(webhook_nudge_module, "REBIND_RETRY_INTERVAL_SECONDS", 0.01)
    clock = Clock()
    server, registry, wait_id = await start_server(
        tmp_path, clock, max_consecutive_accept_failures=2
    )
    loop = asyncio.get_running_loop()

    async def always_fail_accept(sock):
        raise OSError(22, "Invalid argument")

    def always_fail_bind():
        raise OSError(99, "simulated: interface gone")

    monkeypatch.setattr(loop, "sock_accept", always_fail_accept)
    monkeypatch.setattr(server, "_bind_socket", always_fail_bind)

    for _ in range(200):
        if server._sock is None and (
            server._accept_task is None or server._accept_task.done()
        ):
            break
        await asyncio.sleep(0.02)
    # Fail-soft: no live socket, no crash — matches an initial bind failure.
    assert server._sock is None

    # Must still be safely closable even with nothing live to tear down.
    await server.close()


# -- env-gated construction ---------------------------------------------------------


def registry_factory(tmp_path: Path):
    return lambda: ExternalWaitRegistry(tmp_path / "registry.json")


def test_build_from_env_defaults_to_off(tmp_path: Path) -> None:
    assert build_from_env(registry_factory(tmp_path), environ={}) is None
    assert (
        build_from_env(
            registry_factory(tmp_path),
            environ={"CCC_WEBHOOK_NUDGE_ENABLED": "0"},
        )
        is None
    )


def test_build_from_env_without_secret_fails_closed(tmp_path: Path) -> None:
    assert (
        build_from_env(
            registry_factory(tmp_path),
            environ={"CCC_WEBHOOK_NUDGE_ENABLED": "true"},
        )
        is None
    )


def test_build_from_env_constructs_configured_server(tmp_path: Path) -> None:
    server = build_from_env(
        registry_factory(tmp_path),
        environ={
            "CCC_WEBHOOK_NUDGE_ENABLED": "true",
            "CCC_WEBHOOK_NUDGE_SECRET": SECRET,
            "CCC_WEBHOOK_NUDGE_PORT": "0",
            "CCC_WEBHOOK_NUDGE_MAX_BODY_BYTES": "8192",
        },
    )
    assert isinstance(server, WebhookNudgeServer)


def test_build_from_env_tolerates_malformed_numbers(tmp_path: Path) -> None:
    server = build_from_env(
        registry_factory(tmp_path),
        environ={
            "CCC_WEBHOOK_NUDGE_ENABLED": "yes",
            "CCC_WEBHOOK_NUDGE_SECRET": SECRET,
            "CCC_WEBHOOK_NUDGE_PORT": "not-a-port",
            "CCC_WEBHOOK_NUDGE_MAX_BODY_BYTES": "not-a-size",
        },
    )
    assert isinstance(server, WebhookNudgeServer)


def test_secretless_constructor_is_rejected(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(tmp_path / "registry.json")
    with pytest.raises(ValueError):
        WebhookNudgeServer(registry, secret="")
