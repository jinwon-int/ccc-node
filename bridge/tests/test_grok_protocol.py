"""Generated gateway fixtures; no live credentials, prompts or Bot identifiers."""
import copy
import unittest

from telegram_bot.core.grok_protocol import (
    HOST_VERSION, MAX_WIRE, ProtocolError, accepted_prompt, bound_reply,
    capture_baseline, check_host, check_idle, decode_wire, prompt_digest, qualified_hosts,
)

AGENT = "00000000-0000-4000-8000-000000000001"
NONCE = "00000000-0000-4000-8000-000000000002"
PROMPT = "synthetic 한글 request"


class GrokProtocolTests(unittest.TestCase):
    def setUp(self):
        self.old = {"id": "old-reply", "requestId": "old-run"}
        self.baseline = capture_baseline({"entries": [self.old]})
        self.acceptance = {"outcome": "found", "record": {
            "accountSlot": "host", "agentId": AGENT, "clientNonce": NONCE,
            "inputDigest": prompt_digest(AGENT, NONCE, PROMPT),
            "status": "accepted", "echoEntryId": "echo"}}
        self.accepted = accepted_prompt(self.acceptance, AGENT, NONCE, PROMPT)
        self.echo = {"id": "echo", "kind": "message", "role": "user",
                     "content": PROMPT, "clientNonce": NONCE,
                     "requestId": "new-run", "isStreaming": False}
        self.reply = {"id": "answer", "kind": "send-message", "requestId": "new-run",
                      "message": {"type": "text", "content": "synthetic answer"}}
        self.page = {"entries": [self.old, self.echo, self.reply]}
        self.health = {"ok": True, "isBusy": False, "activeAgentId": AGENT}

    def read(self, page=None, health=None, prompt=PROMPT):
        return bound_reply(self.accepted, prompt, self.baseline,
                           self.page if page is None else page,
                           self.health if health is None else health)

    def test_bound_reply_and_ordered_multiple_text(self):
        result = self.read()
        self.assertEqual(result.texts, ("synthetic answer",))
        self.assertEqual(result.request_id, "new-run")
        second = copy.deepcopy(self.reply)
        second["id"] = "answer-2"
        second["message"]["content"] = "synthetic second"
        self.page["entries"].append(second)
        self.assertEqual(self.read().entry_ids, ("answer", "answer-2"))

    def test_empty_initial_history(self):
        self.baseline = capture_baseline({"entries": []})
        self.page["entries"].pop(0)
        self.assertEqual(self.read().texts, ("synthetic answer",))

    def test_empty_baseline_cannot_hide_a_truncated_foreign_prefix(self):
        self.baseline = capture_baseline({"entries": []})
        foreign = {**self.echo, "id": "foreign", "clientNonce": "foreign"}
        full = [foreign, self.echo] + [{**self.reply, "id": f"answer-{i}"} for i in range(63)]
        for page in [{"entries": full[-64:], "nextBeforeSeq": 2},
                     {"entries": full[-64:]},
                     {"entries": [self.echo, self.reply], "nextBeforeSeq": 1}]:
            with self.subTest(count=len(page["entries"])), self.assertRaisesRegex(ProtocolError, "unanchored_range_not_complete"):
                self.read(page)

    def test_wire_denies_duplicate_alias_invalid_utf8_nan_and_depth(self):
        for raw in [b'{"ok":true,"ok":false}', b'{"x":{"a":1,"a":2}}',
                    b'\xff', b'{"value":NaN}', b'{"value":Infinity}',
                    b'[' * 1500 + b']' * 1500, b'', b' ' * (MAX_WIRE + 1)]:
            with self.subTest(raw_size=len(raw)), self.assertRaises(ProtocolError):
                decode_wire(raw)
        self.assertEqual(decode_wire(b'{"ok":true}'), {"ok": True})

    def test_host_version_policy_list_and_capability_only(self):
        valid = {"hostVersion": "0123abc", "isBusy": False,
                 "capabilities": ["sendAcceptanceV1", "orderedReplicasV1"]}
        with self.assertRaisesRegex(ProtocolError, "unqualified_host_version"):
            check_host(valid)  # baseline pin is the default
        self.assertEqual(check_host(valid, frozenset({"0123abc"})), "0123abc")
        self.assertEqual(check_host(valid, None), "0123abc")
        for bad in ("different", "ABCDEF0", "abc", "a" * 41, None, 7):
            with self.subTest(bad=bad), self.assertRaisesRegex(ProtocolError, "unqualified_host_version"):
                check_host({**valid, "hostVersion": bad}, None)
        with self.assertRaisesRegex(ProtocolError, "host_capability_mismatch"):
            check_host({**valid, "capabilities": ["orderedReplicasV1"]}, None)
        self.assertEqual(qualified_hosts(None), frozenset({HOST_VERSION}))
        self.assertEqual(qualified_hosts("  "), frozenset({HOST_VERSION}))
        self.assertIsNone(qualified_hosts("any"))
        self.assertIsNone(qualified_hosts(" ANY "))
        self.assertEqual(qualified_hosts("0123abc, FEDCBA9"), frozenset({HOST_VERSION, "0123abc", "fedcba9"}))
        for bad in ("bad!", "0123abc,,zz", ","):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                qualified_hosts(bad)

    def test_pinned_capabilities_and_idle(self):
        valid = {"hostVersion": HOST_VERSION, "isBusy": False,
                 "capabilities": ["sendAcceptanceV1", "orderedReplicasV1", "voiceSettingsV1"]}
        self.assertEqual(check_host(valid), HOST_VERSION)
        for key, value in [("hostVersion", "different"), ("capabilities", []),
                           ("capabilities", [None]), ("isBusy", 0)]:
            with self.subTest(key=key, value=value), self.assertRaises(ProtocolError):
                check_host({**valid, key: value})
        for change in [{"ok": 1}, {"isBusy": 0}, {"isBusy": True},
                       {"activeAgentId": "other"}, {"busyOnlyAwaitingApproval": True}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                check_idle({**self.health, **change}, AGENT)

    def test_nonce_prompt_and_target_digest_binding(self):
        for key, value in [("accountSlot", "other"), ("clientNonce", AGENT),
                           ("agentId", NONCE), ("inputDigest", "0" * 64),
                           ("status", "pending"), ("status", "rejected"),
                           ("echoEntryId", None)]:
            case = copy.deepcopy(self.acceptance)
            case["record"][key] = value
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                accepted_prompt(case, AGENT, NONCE, PROMPT)
        for outcome in ["unknown-durability", "not-found", "found", None]:
            with self.subTest(outcome=outcome), self.assertRaises(ProtocolError):
                accepted_prompt({"outcome": outcome}, AGENT, NONCE, PROMPT)
        with self.assertRaises(ProtocolError):
            self.read(prompt="changed synthetic input")

    def test_invalid_request_limits_and_uuid(self):
        for prompt in ["", None, "x" * 32769, "한" * 10923, "x\0y", "\ud800"]:
            with self.subTest(kind=type(prompt).__name__), self.assertRaises(ProtocolError):
                prompt_digest(AGENT, NONCE, prompt)
        for value in ["other", AGENT.replace("00000000", "ABCDEFAB"), None, "x\ny"]:
            with self.subTest(kind=type(value).__name__), self.assertRaises(ProtocolError):
                prompt_digest(value, NONCE, PROMPT)

    def test_missing_reordered_and_duplicate_range_denied(self):
        for rows in [[self.echo, self.reply], [self.old, self.reply, self.echo],
                     [self.old, self.echo], [self.old, self.echo, self.reply, self.reply],
                     [self.old] * 65, [None], [{"id": ""}]]:
            with self.subTest(count=len(rows)), self.assertRaises(ProtocolError):
                self.read({"entries": rows})

    def test_old_reply_never_satisfies_a_new_send(self):
        old = copy.deepcopy(self.reply)
        old["requestId"] = "old-run"
        with self.assertRaisesRegex(ProtocolError, "interleaved_run"):
            self.read({"entries": [self.old, self.echo, old]})
        self.echo["requestId"] = "old-run"
        self.reply["requestId"] = "old-run"
        with self.assertRaisesRegex(ProtocolError, "reused_request_id"):
            self.read()

    def test_foreign_input_or_background_output_retires(self):
        foreign = {**self.echo, "id": "foreign", "clientNonce": "other", "content": "foreign"}
        for rows in [[self.old, foreign, self.echo, self.reply],
                     [self.old, self.echo, foreign, self.reply]]:
            with self.assertRaises(ProtocolError):
                self.read({"entries": rows})
        for value in [None, "foreign-run"]:
            case = copy.deepcopy(self.page)
            case["entries"][-1]["requestId"] = value
            with self.assertRaises(ProtocolError):
                self.read(case)

    def test_foreign_input_after_completed_reply_ends_range_and_events_are_ignored(self):
        # Host f7045c4: the owner talks to the same Bot in another client after
        # our run replied, and automation events sit in the tail without a
        # requestId. Neither retires a reply that already exists.
        later = {**self.echo, "id": "later", "clientNonce": "other", "content": "later", "requestId": "later-run"}
        later_reply = {**self.reply, "id": "later-answer", "requestId": "later-run"}
        event = {"id": "event-" + "a" * 64, "kind": "event", "timestampMs": 1,
                 "event": {"type": "automation-changed", "action": "created"}}
        result = self.read({"entries": [self.old, self.echo, event, self.reply, later, event | {"id": "event-" + "b" * 64}, later_reply]})
        self.assertEqual(result.texts, ("synthetic answer",))
        self.assertEqual(result.entry_ids, ("answer",))
        with self.assertRaisesRegex(ProtocolError, "interleaved_run"):
            self.read({"entries": [self.old, self.echo, event, later, self.reply]})

    def test_reply_pending_until_output_exists_and_oversize_is_distinct(self):
        with self.assertRaisesRegex(ProtocolError, "reply_pending"):
            self.read({"entries": [self.old]})  # echo not surfaced yet
        with self.assertRaisesRegex(ProtocolError, "reply_pending"):
            self.read({"entries": [self.old, self.echo]})  # echo, no output yet
        streaming = copy.deepcopy(self.reply); streaming["isStreaming"] = True
        with self.assertRaisesRegex(ProtocolError, "reply_pending"):
            self.read({"entries": [self.old, self.echo, streaming]})
        big = copy.deepcopy(self.reply); big["message"]["content"] = "x" * 40000
        big2 = copy.deepcopy(big); big2["id"] = "answer-2"
        with self.assertRaisesRegex(ProtocolError, "reply_oversize"):
            self.read({"entries": [self.old, self.echo, big, big2]})

    def test_acceptance_without_reported_digest_is_bound_by_echo(self):
        blank = copy.deepcopy(self.acceptance)
        blank["record"]["inputDigest"] = ""
        accepted = accepted_prompt(blank, AGENT, NONCE, PROMPT)
        self.assertEqual(accepted.digest, prompt_digest(AGENT, NONCE, PROMPT))
        self.assertEqual(self.read().texts, ("synthetic answer",))
        for bad in (None, 7, "0" * 64):
            case = copy.deepcopy(self.acceptance)
            case["record"]["inputDigest"] = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(ProtocolError, "acceptance_binding_mismatch"):
                accepted_prompt(case, AGENT, NONCE, PROMPT)

    def test_echo_substitution_and_streaming_denied(self):
        for key, value in [("clientNonce", AGENT), ("content", "secret-not-for-error"),
                           ("role", "assistant"), ("kind", "other"),
                           ("requestId", ""), ("isStreaming", True)]:
            case = copy.deepcopy(self.page)
            case["entries"][1][key] = value
            with self.subTest(key=key), self.assertRaises(ProtocolError) as error:
                self.read(case)
            self.assertNotIn("secret-not-for-error", str(error.exception))

    def test_tool_approval_attachment_and_channel_output_not_text(self):
        for message in [{"type": "approval", "approval": {}},
                        {"type": "attachment", "path": "/synthetic"},
                        {"type": "text", "content": "x", "channel": "external"},
                        {"type": "text", "content": "x", "images": []},
                        {"type": "text", "content": "x" * 65537}]:
            case = copy.deepcopy(self.page)
            case["entries"][-1]["message"] = message
            with self.subTest(kind=message["type"]), self.assertRaises(ProtocolError):
                self.read(case)
        self.reply["author"] = {"id": "foreign-author"}
        with self.assertRaises(ProtocolError):
            self.read()

    def test_total_output_cap(self):
        self.reply["message"]["content"] = "x" * 40000
        self.page["entries"].append({**self.reply, "id": "answer-2"})
        with self.assertRaises(ProtocolError):
            self.read()

    def test_malformed_optional_flags_never_release_text(self):
        for value in [True, 1, 0, "true", "false", [], {}, None]:
            case = copy.deepcopy(self.page)
            case["entries"][-1]["isStreaming"] = value
            with self.subTest(flag="isStreaming", kind=type(value).__name__), self.assertRaises(ProtocolError):
                self.read(case)
            with self.subTest(flag="busyOnlyAwaitingApproval", kind=type(value).__name__), self.assertRaises(ProtocolError):
                self.read(health={**self.health, "busyOnlyAwaitingApproval": value})
        self.reply["isStreaming"] = False
        self.assertEqual(self.read(health={**self.health, "busyOnlyAwaitingApproval": False}).texts,
                         ("synthetic answer",))

    def test_json_numeric_overflow_is_bounded_and_body_free(self):
        for raw in [b'{"n":' + b'9' * 5000 + b'}', b'{"n":1e999}', b'{"n":-1e999}']:
            with self.subTest(size=len(raw)), self.assertRaises(ProtocolError) as error:
                decode_wire(raw)
            self.assertLess(len(str(error.exception)), 80)
        self.assertEqual(decode_wire(b'{"n":9223372036854775807,"f":1.5}'),
                         {"n": 9223372036854775807, "f": 1.5})


if __name__ == "__main__":
    unittest.main()
