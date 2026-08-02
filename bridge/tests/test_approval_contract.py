"""Provider-neutral, body-safe approval snapshot regressions (#870)."""

from __future__ import annotations

from telegram_bot.core.agent_runtime import ApprovalRequestEvent
from telegram_bot.core.approval_contract import build_approval_snapshot


def _command_event(secret: str) -> ApprovalRequestEvent:
    return ApprovalRequestEvent(
        "request-1",
        "item/commandExecution/requestApproval",
        {
            "command": (
                "\x1b[31mcurl https://example.invalid "
                f"Authorization: Bearer {secret} FOO=private-value "
                'PASSWORD="quoted private" --token "cli private" '
                "mysql -pinline-private\n"
                "private file body must not be displayed"
            ),
            "cwd": "/srv/ccc-node/bridge",
            "environment": {"RAW_VALUE": "never-display-this"},
            "headers": {"Authorization": f"Bearer {secret}"},
        },
        "provider description containing never-display-this",
    )


def test_request_and_exact_display_fingerprints_are_separate() -> None:
    first = build_approval_snapshot(_command_event("A" * 32))
    second = build_approval_snapshot(_command_event("B" * 32))

    assert first.request_fingerprint != second.request_fingerprint
    assert first.display_fingerprint == second.display_fingerprint
    assert first.request_fingerprint.startswith("hmac-sha256:")
    assert first.display_fingerprint.startswith("sha256:")
    assert first.provider == "codex"
    assert first.action == "command_execution"
    assert first.target_shape == "command"
    assert set(first.redaction_flags) >= {
        "authorization_redacted",
        "body_omitted",
        "controls_removed",
        "environment_redacted",
        "sensitive_fields_omitted",
    }
    for forbidden in (
        "A" * 32,
        "B" * 32,
        "private-value",
        "quoted private",
        "cli private",
        "inline-private",
        "never-display-this",
        "private file body",
        "\x1b",
    ):
        assert forbidden not in first.prompt_text
        assert forbidden not in second.prompt_text
    assert "Risk hints: network, absolute-path" in first.prompt_text


def test_unicode_line_separators_cannot_bypass_first_line_omission() -> None:
    event = ApprovalRequestEvent(
        "unicode-lines",
        "Bash",
        {"command": "echo visible\u2028plain-secret-second-line\u2029third-line"},
        "run multiline command",
    )

    snapshot = build_approval_snapshot(event)

    assert snapshot.summary == "echo visible …"
    assert "body_omitted" in snapshot.redaction_flags
    assert "controls_removed" in snapshot.redaction_flags
    assert "plain-secret-second-line" not in snapshot.prompt_text
    assert "third-line" not in snapshot.prompt_text


def test_service_prefixed_sensitive_options_are_fully_redacted() -> None:
    event = ApprovalRequestEvent(
        "prefixed-options",
        "Bash",
        {
            "command": (
                'deploy --client-secret "client plain" '
                "--github-token github-plain --db-password dbplain "
                "--aws-secret-access-key awsplain --slack_bot_token slackplain"
            )
        },
        "deploy",
    )

    snapshot = build_approval_snapshot(event)

    for forbidden in (
        "client plain",
        "github-plain",
        "dbplain",
        "awsplain",
        "slackplain",
    ):
        assert forbidden not in snapshot.prompt_text
    assert snapshot.summary.count("[REDACTED_CREDENTIAL]") == 5


def test_custom_authorization_schemes_never_leave_credentials() -> None:
    event = ApprovalRequestEvent(
        "custom-authorization",
        "ITEM/CommandExecution/RequestApproval",
        {
            "command": (
                'curl -H "Authorization: Token quoted-secret" '
                "https://example.invalid; printf Authorization: Custom "
                "unquoted-secret second-secret"
            )
        },
        "custom authorization headers",
    )

    snapshot = build_approval_snapshot(event)

    assert snapshot.provider == "codex"
    assert snapshot.action == "command_execution"
    assert snapshot.summary == (
        'curl -H "[REDACTED_CREDENTIAL]" https://example.invalid; '
        "printf Authorization: [REDACTED_CREDENTIAL]"
    )
    for forbidden in ("Token", "quoted-secret", "Custom", "unquoted-secret", "second-secret"):
        assert forbidden not in snapshot.prompt_text
    assert "authorization_redacted" in snapshot.redaction_flags


def test_file_snapshot_shows_only_bounded_target_not_body() -> None:
    event = ApprovalRequestEvent(
        "request-file",
        "item/fileChange/requestApproval",
        {
            "path": "/workspace/project/report.txt",
            "content": "sk-" + "x" * 40,
            "patch": "raw patch body",
        },
        "write secret body",
    )

    snapshot = build_approval_snapshot(event)

    assert snapshot.action == "file_change"
    assert snapshot.summary == "/workspace/project/report.txt"
    assert "body_omitted" in snapshot.redaction_flags
    assert "sensitive_fields_omitted" in snapshot.redaction_flags
    assert "raw patch" not in snapshot.prompt_text
    assert "sk-" not in snapshot.prompt_text


def test_claude_tool_converges_on_same_snapshot_schema_and_bounds() -> None:
    event = ApprovalRequestEvent(
        "claude-request",
        "Bash",
        {"command": "pytest -q " + "x" * 800},
        "Claude wants to run tests",
    )

    snapshot = build_approval_snapshot(event)

    assert snapshot.provider == "claude"
    assert snapshot.action == "command_execution"
    assert snapshot.target_shape == "command"
    assert "truncated" in snapshot.redaction_flags
    assert len(snapshot.summary.encode("utf-8")) <= 240
    assert snapshot.prompt_text.startswith("Claude approval request\n")
    assert len(snapshot.prompt_text.encode("utf-8")) < 1024


def test_permission_snapshot_lists_names_without_values() -> None:
    event = ApprovalRequestEvent(
        "permission-request",
        "item/permissions/requestApproval",
        {"permissions": {"network": {"token": "do-not-show"}, "filesystem": True}},
        "grant permissions",
    )

    snapshot = build_approval_snapshot(event)

    assert snapshot.action == "permissions"
    assert snapshot.summary == "filesystem, network"
    assert "do-not-show" not in snapshot.prompt_text
