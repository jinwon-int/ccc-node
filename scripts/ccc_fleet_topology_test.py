#!/usr/bin/env python3
"""Direct unit tests for the offline fleet inventory validator (#1451).

Run standalone: python3 scripts/ccc_fleet_topology_test.py

Every filesystem case runs under a private fixture root created with mode
0700 inside the caller's temporary directory. That root stands in for the
caller-trusted protected root the contract requires; traversal below it is
what the validator checks. The suite deliberately does NOT relax the parent
rules so a shared world-writable ancestry would pass — the opposite is
asserted in ``test_world_writable_sticky_parent_is_refused`` and
``test_traversal_from_filesystem_root_refuses_shared_tmp``.

All identities, endpoints and paths below are synthetic examples.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ccc_fleet_topology as topology

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = REPO_ROOT / "docs" / "examples" / "fleet-topology.example.json"

SENTINEL_ALIAS = "sentinel-alias-node"
SENTINEL_ENDPOINT = "https://sentinel-endpoint.private-fixture.internal:8443/sentinel"
SENTINEL_REPO_ROOT = "/opt/sentinel-repo-root"
SENTINEL_KEY_REF = "worker:sentinel-alias-node:g7:v9"


def _node(**overrides: object) -> dict[str, object]:
    node: dict[str, object] = {
        "alias": "fixture-node",
        "platform": "linux-systemd",
        "roles": ["a2a-worker"],
        "repoRoot": "/opt/ccc-node",
        "endpoint": "https://fixture-node.private-fixture.internal:8443/a2a",
        "keyRef": "worker:fixture-node:g1:v1",
        "enabled": True,
    }
    node.update(overrides)
    return node


def _document(*nodes: dict[str, object], version: object = 1) -> dict[str, object]:
    return {"version": version, "nodes": list(nodes) or [_node()]}


def _payload(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


def _trust(**overrides: object) -> topology.DescriptorTrust:
    fields: dict[str, object] = {"file_uids": frozenset({os.getuid()})}
    fields.update(overrides)
    return topology.DescriptorTrust(**fields)  # type: ignore[arg-type]


@contextlib.contextmanager
def _fixture_root():
    """A private, owner-only fixture root standing in for a protected root."""

    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / "private-root"
        root.mkdir()
        os.chmod(root, 0o700)
        yield root


def _write_descriptor(root: Path, name: str = "topology.json", body: bytes | None = None) -> Path:
    target = root / name
    target.write_bytes(_payload(_document()) if body is None else body)
    os.chmod(target, 0o600)
    return target


def _read(root: Path, relative: str, trust: topology.DescriptorTrust | None = None) -> bytes:
    with topology.trusted_root(root) as root_fd:
        return topology.read_descriptor(root_fd, relative, trust or _trust())


def _read_code(root: Path, relative: str, trust: topology.DescriptorTrust | None = None) -> str:
    with topology.trusted_root(root) as root_fd:
        try:
            topology.read_descriptor(root_fd, relative, trust or _trust())
        except topology.TopologyError as error:
            return error.finding.code
    return ""


class DescriptorMetadataTests(unittest.TestCase):
    def test_accepts_owner_only_regular_file_under_a_trusted_root(self) -> None:
        with _fixture_root() as root:
            _write_descriptor(root)
            self.assertEqual(json.loads(_read(root, "topology.json"))["version"], 1)

    def test_nested_traversal_validates_every_parent_on_its_descriptor(self) -> None:
        with _fixture_root() as root:
            nested = root / "ccc-node"
            nested.mkdir()
            os.chmod(nested, 0o700)
            _write_descriptor(nested)
            self.assertTrue(_read(root, "ccc-node/topology.json"))

    def test_symlinked_descriptor_is_refused(self) -> None:
        with _fixture_root() as root:
            _write_descriptor(root, "real.json")
            os.symlink(root / "real.json", root / "topology.json")
            self.assertEqual(_read_code(root, "topology.json"), "descriptor_symlink")

    def test_symlinked_parent_is_refused(self) -> None:
        with _fixture_root() as root:
            real = root / "real-dir"
            real.mkdir()
            os.chmod(real, 0o700)
            _write_descriptor(real)
            os.symlink(real, root / "link-dir")
            self.assertEqual(
                _read_code(root, "link-dir/topology.json"), "descriptor_parent_symlink"
            )

    def test_absolute_and_parent_components_are_refused(self) -> None:
        with _fixture_root() as root:
            _write_descriptor(root)
            self.assertEqual(
                _read_code(root, "/etc/ccc-node/topology.json"),
                "descriptor_path_not_relative",
            )
            self.assertEqual(_read_code(root, ""), "descriptor_path_not_relative")
            for relative in ("../topology.json", "a/../topology.json", "./topology.json",
                             "a//topology.json", ".hidden/topology.json"):
                self.assertEqual(
                    _read_code(root, relative), "descriptor_path_component", relative
                )

    def test_fifo_is_refused_without_blocking(self) -> None:
        with _fixture_root() as root:
            os.mkfifo(root / "topology.json", 0o600)
            # A blocking open would hang forever with no writer; the alarm turns
            # that regression into a failure instead of a stuck suite.
            signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(AssertionError("open blocked on FIFO")))
            signal.alarm(5)
            try:
                self.assertEqual(_read_code(root, "topology.json"), "descriptor_not_regular")
            finally:
                signal.alarm(0)

    def test_directory_and_socket_are_refused(self) -> None:
        with _fixture_root() as root:
            (root / "topology.json").mkdir()
            os.chmod(root / "topology.json", 0o700)
            self.assertEqual(_read_code(root, "topology.json"), "descriptor_not_regular")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(root / "sock.json"))
                self.assertEqual(_read_code(root, "sock.json"), "descriptor_not_regular")
            finally:
                server.close()

    @unittest.skipUnless(
        os.path.exists("/dev/null") and stat.S_ISCHR(os.stat("/dev/null").st_mode),
        "no character device available",
    )
    def test_character_device_is_refused_without_blocking(self) -> None:
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(AssertionError("open blocked on device")))
        signal.alarm(5)
        try:
            # /dev is only read here: the device must be rejected on its own
            # descriptor rather than opened and consumed.
            self.assertEqual(
                _read_code(Path("/dev"), "null", _trust(file_uids=frozenset({0}))),
                "descriptor_not_regular",
            )
        finally:
            signal.alarm(0)

    def test_untrusted_parent_owner_is_refused(self) -> None:
        with _fixture_root() as root:
            nested = root / "ccc-node"
            nested.mkdir()
            os.chmod(nested, 0o700)
            _write_descriptor(nested)
            trust = _trust(parent_uids=frozenset({os.getuid() + 1}))
            self.assertEqual(
                _read_code(root, "ccc-node/topology.json", trust),
                "descriptor_parent_untrusted_owner",
            )

    def test_group_or_other_writable_parent_is_refused(self) -> None:
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)), _fixture_root() as root:
                nested = root / "ccc-node"
                nested.mkdir()
                _write_descriptor(nested)
                os.chmod(nested, mode)
                self.assertEqual(
                    _read_code(root, "ccc-node/topology.json"),
                    "descriptor_parent_writable",
                )

    def test_world_writable_sticky_parent_is_refused(self) -> None:
        # The sticky bit does not make a shared directory a trusted ancestry.
        with _fixture_root() as root:
            nested = root / "shared"
            nested.mkdir()
            _write_descriptor(nested)
            os.chmod(nested, 0o1777)
            self.assertEqual(
                _read_code(root, "shared/topology.json"), "descriptor_parent_writable"
            )

    @unittest.skipUnless(
        os.path.isdir("/tmp") and os.stat("/tmp").st_mode & 0o022,
        "/tmp is not shared-writable on this host",
    )
    def test_traversal_from_filesystem_root_refuses_shared_tmp(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as base:
            target = Path(base) / "topology.json"
            target.write_bytes(_payload(_document()))
            os.chmod(target, 0o600)
            relative = str(target.relative_to("/"))
            self.assertEqual(
                _read_code(Path("/"), relative, _trust(parent_uids=frozenset({0, os.getuid()}))),
                "descriptor_parent_writable",
            )

    def test_non_0600_mode_is_refused(self) -> None:
        for mode in (0o644, 0o640, 0o604, 0o700, 0o666):
            with self.subTest(mode=oct(mode)), _fixture_root() as root:
                target = _write_descriptor(root)
                os.chmod(target, mode)
                self.assertEqual(_read_code(root, "topology.json"), "descriptor_mode")

    def test_special_mode_bits_are_refused(self) -> None:
        for mode in (0o4600, 0o2600, 0o1600):
            with self.subTest(mode=oct(mode)), _fixture_root() as root:
                target = _write_descriptor(root)
                os.chmod(target, mode)
                self.assertEqual(
                    _read_code(root, "topology.json"), "descriptor_special_bits"
                )

    def test_untrusted_file_owner_is_refused(self) -> None:
        with _fixture_root() as root:
            _write_descriptor(root)
            trust = _trust(file_uids=frozenset({os.getuid() + 1}))
            self.assertEqual(
                _read_code(root, "topology.json", trust), "descriptor_untrusted_owner"
            )

    def test_additional_hard_link_is_refused(self) -> None:
        with _fixture_root() as root:
            target = _write_descriptor(root)
            os.link(target, root / "second.json")
            self.assertEqual(
                _read_code(root, "topology.json"), "descriptor_multiple_links"
            )

    def test_byte_limit_is_16384(self) -> None:
        with _fixture_root() as root:
            _write_descriptor(root, body=b"x" * topology.MAX_DESCRIPTOR_BYTES)
            self.assertEqual(len(_read(root, "topology.json")), 16384)
            _write_descriptor(root, body=b"x" * (topology.MAX_DESCRIPTOR_BYTES + 1))
            self.assertEqual(_read_code(root, "topology.json"), "descriptor_too_large")

    def test_missing_descriptor_is_reported_without_fallback(self) -> None:
        with _fixture_root() as root:
            self.assertEqual(_read_code(root, "topology.json"), "descriptor_missing")
            with topology.trusted_root(root) as root_fd:
                report = topology.read_and_validate_structural(
                    root_fd, "topology.json", _trust()
                )
            self.assertEqual(report.codes(), ("descriptor_missing",))
            self.assertFalse(report.ok)
            self.assertIsNone(report.document)

    def test_validation_never_writes_to_the_fixture_root(self) -> None:
        with _fixture_root() as root:
            _write_descriptor(root)
            before = {
                entry: os.stat(root / entry).st_mtime_ns for entry in os.listdir(root)
            }
            with topology.trusted_root(root) as root_fd:
                topology.read_and_validate_structural(root_fd, "topology.json", _trust())
            after = {
                entry: os.stat(root / entry).st_mtime_ns for entry in os.listdir(root)
            }
            self.assertEqual(before, after)


class SyntaxTests(unittest.TestCase):
    def test_valid_document_decodes(self) -> None:
        self.assertEqual(topology.decode_descriptor(_payload(_document()))["version"], 1)

    def test_rejects_non_bytes_input(self) -> None:
        with self.assertRaises(topology.TopologyError) as caught:
            topology.decode_descriptor(json.dumps(_document()))  # type: ignore[arg-type]
        self.assertEqual(caught.exception.finding.code, "syntax_not_bytes")

    def _code(self, payload: bytes) -> str:
        with self.assertRaises(topology.TopologyError) as caught:
            topology.decode_descriptor(payload)
        return caught.exception.finding.code

    def test_rejects_oversize_input(self) -> None:
        self.assertEqual(
            self._code(b"{}" + b" " * topology.MAX_DESCRIPTOR_BYTES),
            "syntax_byte_limit_exceeded",
        )

    def test_rejects_invalid_utf8_and_bom(self) -> None:
        self.assertEqual(self._code(b'{"version": "\xff\xfe"}'), "syntax_invalid_utf8")
        self.assertEqual(
            self._code("﻿".encode("utf-8") + b"{}"), "syntax_bom_forbidden"
        )

    def test_rejects_duplicate_keys(self) -> None:
        self.assertEqual(
            self._code(b'{"version": 1, "version": 1, "nodes": []}'),
            "syntax_duplicate_key",
        )

    def test_rejects_float_lexical_forms_including_version_1_0(self) -> None:
        self.assertEqual(
            self._code(b'{"version": 1.0, "nodes": []}'), "syntax_float_forbidden"
        )
        self.assertEqual(
            self._code(b'{"version": 1e0, "nodes": []}'), "syntax_float_forbidden"
        )

    def test_rejects_nan_and_infinity(self) -> None:
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            self.assertEqual(
                self._code(b'{"version": ' + literal + b', "nodes": []}'),
                "syntax_constant_forbidden",
            )

    def test_enforces_bounded_int64_profile(self) -> None:
        largest = str(2**63 - 1).encode("ascii")
        self.assertEqual(
            topology.decode_descriptor(b'{"n": ' + largest + b"}")["n"], 2**63 - 1
        )
        self.assertEqual(
            self._code(b'{"n": ' + str(2**63).encode("ascii") + b"}"),
            "syntax_integer_out_of_range",
        )
        self.assertEqual(
            self._code(b'{"n": -' + str(2**63 + 1).encode("ascii") + b"}"),
            "syntax_integer_out_of_range",
        )
        self.assertEqual(
            self._code(b'{"n": ' + b"9" * 40 + b"}"), "syntax_integer_out_of_range"
        )

    def test_enforces_depth_limit_of_eight(self) -> None:
        eight = b'{"a":' * 7 + b"1" + b"}" * 7
        self.assertEqual(topology.decode_descriptor(eight)["a"]["a"]["a"]["a"]["a"]["a"]["a"], 1)
        nine = b'{"a":' * 9 + b"1" + b"}" * 9
        self.assertEqual(self._code(nine), "syntax_depth_exceeded")
        self.assertEqual(self._code(b'{"a": ' + b"[" * 9 + b"]" * 9 + b"}"), "syntax_depth_exceeded")

    def test_braces_inside_strings_are_not_nesting(self) -> None:
        self.assertEqual(topology.decode_descriptor(b'{"a": "{{{{{{{{{{"}')["a"], "{" * 10)

    def test_rejects_trailing_data_and_non_object_roots(self) -> None:
        self.assertEqual(self._code(b'{"version": 1} {"version": 1}'), "syntax_invalid")
        self.assertEqual(self._code(b"[]"), "syntax_root_not_object")
        self.assertEqual(self._code(b"1"), "syntax_root_not_object")

    def test_rejects_unpaired_surrogates(self) -> None:
        self.assertEqual(
            self._code(b'{"a": "\\ud800"}'), "syntax_surrogate_forbidden"
        )


class StructureTests(unittest.TestCase):
    def _codes(self, document: object) -> tuple[str, ...]:
        return topology.validate_structural(_payload(document)).codes()

    def test_checked_in_example_is_structurally_valid(self) -> None:
        report = topology.validate_structural(EXAMPLE_PATH.read_bytes())
        self.assertEqual(report.findings, ())
        self.assertTrue(report.ok)

    def test_unknown_keys_are_rejected_at_every_level(self) -> None:
        document = _document()
        document["extra"] = 1
        self.assertEqual(self._codes(document), ("structure_unknown_key",))
        self.assertEqual(
            self._codes(_document(_node(extra=1))), ("structure_unknown_key",)
        )

    def test_version_must_be_the_integer_one(self) -> None:
        self.assertEqual(self._codes(_document(version=2)), ("structure_const",))
        self.assertEqual(self._codes(_document(version="1")), ("structure_type",))
        self.assertEqual(self._codes(_document(version=True)), ("structure_type",))

    def test_closed_enums_are_enforced(self) -> None:
        self.assertEqual(
            self._codes(_document(_node(platform="darwin"))), ("structure_enum",)
        )
        self.assertEqual(
            self._codes(_document(_node(roles=["a2a-relay"]))), ("structure_enum",)
        )

    def test_duplicate_roles_are_rejected(self) -> None:
        self.assertEqual(
            self._codes(_document(_node(roles=["a2a-worker", "a2a-worker"]))),
            ("structure_duplicate_items",),
        )

    def test_empty_roles_array_is_legal(self) -> None:
        self.assertEqual(self._codes(_document(_node(roles=[]))), ())

    def test_missing_required_fields_are_rejected(self) -> None:
        for field in ("alias", "platform", "roles", "repoRoot", "enabled"):
            node = _node()
            del node[field]
            with self.subTest(field=field):
                self.assertEqual(self._codes(_document(node)), ("structure_required",))

    def test_enabled_absence_is_never_an_implied_true(self) -> None:
        node = _node()
        del node["enabled"]
        report = topology.validate_structural(_payload(_document(node)))
        self.assertFalse(report.ok)
        self.assertIsNone(report.document)

    def test_wrong_types_are_rejected(self) -> None:
        self.assertEqual(self._codes(_document(_node(enabled=1))), ("structure_type",))
        self.assertEqual(self._codes(_document(_node(enabled="true"))), ("structure_type",))
        self.assertEqual(self._codes(_document(_node(roles="a2a-worker"))), ("structure_type",))
        self.assertEqual(self._codes({"version": 1, "nodes": {}}), ("structure_type",))

    def test_node_count_bounds(self) -> None:
        self.assertEqual(self._codes({"version": 1, "nodes": []}), ("structure_min_items",))

    def test_alias_pattern_is_strict(self) -> None:
        for alias in ("1node", "-node", "Node", "node@host", "node/child", "n",
                      "node.name", "node name", "a" * 33):
            with self.subTest(alias=alias):
                self.assertIn("structure_", self._codes(_document(_node(alias=alias)))[0])

    def test_trailing_newline_is_rejected_in_every_patterned_field(self) -> None:
        for field, value in (
            ("alias", "fixture-node\n"),
            ("repoRoot", "/opt/ccc-node\n"),
            ("endpoint", "https://fixture-node.private-fixture.internal\n"),
            ("keyRef", "worker:fixture-node:g1:v1\n"),
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    self._codes(_document(_node(**{field: value}))), ("structure_pattern",)
                )

    def test_repo_root_pattern_is_strict(self) -> None:
        for value in ("opt/ccc-node", "/opt/ccc-node/", "/opt//ccc-node", "/opt/.hidden",
                      "/opt/../etc", "/"):
            with self.subTest(value=value):
                self.assertEqual(
                    self._codes(_document(_node(repoRoot=value)))[0][:10], "structure_"
                )

    def test_endpoint_screen_rejects_userinfo_query_fragment_and_controls(self) -> None:
        for value in (
            "https://user@fixture-node.internal",
            "https://fixture-node.internal?a=b",
            "https://fixture-node.internal#f",
            "https://fixture-node .internal",
            "https://fixture-node.internal\x01",
            "ftp://fixture-node.internal",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self._codes(_document(_node(endpoint=value))), ("structure_pattern",)
                )

    def test_max_length_is_enforced_independently_of_the_pattern(self) -> None:
        long_endpoint = "https://" + "a" * 2048 + ".internal"
        self.assertEqual(
            self._codes(_document(_node(endpoint=long_endpoint))), ("structure_max_length",)
        )

    def test_key_ref_pattern_is_strict(self) -> None:
        for value in ("worker:fixture-node:g1", "broker:fixture-node:g1:v1",
                      "worker:fixture-node:g1234:v1", "worker:fixture-node:gx:v1"):
            with self.subTest(value=value):
                self.assertEqual(
                    self._codes(_document(_node(keyRef=value))), ("structure_pattern",)
                )

    def test_schema_keyword_drift_fails_closed(self) -> None:
        with self.assertRaises(topology.TopologyError) as caught:
            topology._validate_schema_node(1, {"multipleOf": 2}, "")
        self.assertEqual(caught.exception.finding.code, "schema_unsupported_keyword")
        with self.assertRaises(topology.TopologyError) as caught:
            topology._validate_schema_node({}, {"additionalProperties": {}}, "")
        self.assertEqual(caught.exception.finding.code, "schema_unsupported_keyword")

    def test_structural_failure_rejects_the_whole_document(self) -> None:
        report = topology.validate_structural(
            _payload(_document(_node(), _node(alias="Bad-Alias")))
        )
        self.assertFalse(report.ok)
        self.assertIsNone(report.document)


class EndpointParsingTests(unittest.TestCase):
    def _code(self, value: str) -> str:
        with self.assertRaises(topology.TopologyError) as caught:
            topology.parse_endpoint(value)
        return caught.exception.finding.code

    def test_accepts_dns_ipv4_and_ipv6_destinations(self) -> None:
        facts = topology.parse_endpoint("https://fixture-node.private-fixture.internal:8443/a2a")
        self.assertEqual((facts.scheme, facts.host_kind, facts.port, facts.path),
                         ("https", "dns", 8443, "/a2a"))
        self.assertFalse(facts.reserved_example)
        loopback = topology.parse_endpoint("http://127.0.0.1:8791/a2a")
        self.assertEqual((loopback.host_kind, loopback.port), ("ipv4", 8791))
        six = topology.parse_endpoint("https://[fd00::1]:8443/api/v1")
        self.assertEqual((six.host_kind, six.host, six.port), ("ipv6", "fd00::1", 8443))
        self.assertIsNone(topology.parse_endpoint("https://fixture-node.internal").port)

    def test_rejects_non_http_schemes_and_relative_forms(self) -> None:
        self.assertEqual(self._code("ftp://fixture-node.internal"), "endpoint_scheme")
        self.assertEqual(self._code("fixture-node.internal"), "endpoint_scheme")
        self.assertEqual(self._code("//fixture-node.internal"), "endpoint_scheme")

    def test_rejects_userinfo_query_fragment_and_controls(self) -> None:
        self.assertEqual(self._code("https://u:p@fixture-node.internal"), "endpoint_userinfo")
        self.assertEqual(self._code("https://fixture-node.internal/?a=b"), "endpoint_query")
        self.assertEqual(self._code("https://fixture-node.internal#frag"), "endpoint_fragment")
        self.assertEqual(topology.parse_endpoint("https://fixture-node.internal/").path, "/")
        self.assertEqual(self._code("https://fixture node.internal"), "endpoint_control_character")
        self.assertEqual(self._code("https://fixture-node.internal\n"), "endpoint_control_character")

    def test_rejects_encoded_authority_delimiters(self) -> None:
        self.assertEqual(self._code("https://fixture%40node.internal"), "endpoint_encoded_delimiter")
        self.assertEqual(self._code("https://fixture%2fnode.internal"), "endpoint_encoded_delimiter")

    def test_rejects_a_destination_that_would_silently_normalize(self) -> None:
        for value in ("HTTPS://fixture-node.internal", "https://FIXTURE-NODE.internal",
                      "https://[2001:0db8::1]/", "https://fixture-node.internal/a/../b",
                      "https://fixture-node.internal?"):
            with self.subTest(value=value):
                self.assertIn(
                    self._code(value), {"endpoint_not_normalized", "endpoint_path", "endpoint_query"}
                )
        self.assertEqual(self._code("HTTPS://fixture-node.internal"), "endpoint_not_normalized")
        self.assertEqual(self._code("https://fixture-node.internal/a/../b"), "endpoint_path")
        self.assertEqual(self._code("https://fixture-node.internal/a//b"), "endpoint_path")

    def test_rejects_invalid_ip_literals(self) -> None:
        self.assertEqual(self._code("https://192.168.001.1"), "endpoint_ip_invalid")
        self.assertEqual(self._code("https://1.2.3.4.5"), "endpoint_ip_invalid")
        self.assertEqual(self._code("https://999.1.1.1"), "endpoint_ip_invalid")
        # The stdlib URL parser refuses these authorities before this module
        # sees them; the refusal still surfaces as a stable public code.
        for value in ("https://[not-an-address]", "https://[fd00::1", "https://[fd00::1]x"):
            with self.subTest(value=value):
                self.assertEqual(self._code(value), "endpoint_invalid")

    def test_rejects_invalid_dns_syntax(self) -> None:
        self.assertEqual(self._code("https://"), "endpoint_host_empty")
        self.assertEqual(self._code("https://fixture..internal"), "endpoint_label_length")
        self.assertEqual(self._code("https://fixture-node.internal."), "endpoint_label_length")
        self.assertEqual(self._code("https://" + "a" * 64 + ".internal"), "endpoint_label_length")
        self.assertEqual(self._code("https://-fixture.internal"), "endpoint_label_syntax")
        self.assertEqual(self._code("https://fixture-.internal"), "endpoint_label_syntax")
        self.assertEqual(
            self._code("https://" + ".".join(["a" * 63] * 4)), "endpoint_host_too_long"
        )

    def test_rejects_out_of_range_and_non_canonical_ports(self) -> None:
        for value in ("https://fixture.internal:0", "https://fixture.internal:65536",
                      "https://fixture.internal:08443", "https://fixture.internal:",
                      "https://fixture.internal:99999999", "https://fixture.internal:https"):
            with self.subTest(value=value):
                self.assertEqual(self._code(value), "endpoint_port")
        self.assertEqual(topology.parse_endpoint("https://fixture.internal:65535").port, 65535)

    def test_rejects_oversized_endpoints(self) -> None:
        self.assertEqual(
            self._code("https://fixture.internal/" + "a" * topology.MAX_ENDPOINT_CHARS),
            "endpoint_too_long",
        )

    def test_flags_reserved_example_destinations(self) -> None:
        for value in ("https://node-alpha.tailnet-placeholder.invalid",
                      "https://node.example.com", "https://node.test", "https://node.localhost",
                      "https://192.0.2.10", "https://[2001:db8::1]"):
            with self.subTest(value=value):
                self.assertTrue(topology.parse_endpoint(value).reserved_example)
        self.assertFalse(
            topology.parse_endpoint("https://fixture-node.internal").reserved_example
        )


class SemanticTests(unittest.TestCase):
    def _codes(self, document: object) -> tuple[str, ...]:
        return topology.validate_structural(_payload(document)).codes()

    def test_aliases_must_be_unique(self) -> None:
        duplicate = _document(_node(), _node(keyRef="worker:fixture-node:g2:v1"))
        self.assertEqual(self._codes(duplicate), ("semantic_duplicate_alias",))

    def test_key_ref_node_segment_must_equal_the_alias(self) -> None:
        self.assertEqual(
            self._codes(_document(_node(keyRef="worker:other-node:g1:v1"))),
            ("semantic_key_ref_alias_mismatch",),
        )

    def test_semantic_endpoint_parsing_runs_beyond_the_schema_screen(self) -> None:
        # The coarse schema pattern accepts these; the real parser must not.
        for value, code in (
            ("https://192.168.001.1", "endpoint_ip_invalid"),
            ("https://fixture.internal:0", "endpoint_port"),
            ("https://FIXTURE.internal", "endpoint_not_normalized"),
            ("https://fixture..internal", "endpoint_label_length"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    topology.validate_structural(
                        _payload(_document(_node(endpoint=value)))
                    ).codes(),
                    (code,),
                )

    def test_valid_document_exposes_the_parsed_document(self) -> None:
        report = topology.validate_structural(_payload(_document()))
        self.assertTrue(report.ok)
        self.assertIsNotNone(report.document)
        self.assertEqual(report.document["nodes"][0]["alias"], "fixture-node")


class OperationalTests(unittest.TestCase):
    def _context(self, **overrides: object) -> topology.OperationalContext:
        fields: dict[str, object] = {
            "local_alias": "fixture-node",
            "selected_aliases": frozenset({"fixture-node"}),
            "endpoint_policy": lambda facts: True,
            "keyring_resolver": lambda key_ref: True,
        }
        fields.update(overrides)
        return topology.OperationalContext(**fields)  # type: ignore[arg-type]

    def _codes(self, document: object, **overrides: object) -> tuple[str, ...]:
        return topology.validate_operational(
            _payload(document), self._context(**overrides)
        ).codes()

    def test_structural_mode_never_claims_operational_readiness(self) -> None:
        report = topology.validate_structural(EXAMPLE_PATH.read_bytes())
        self.assertTrue(report.ok)
        self.assertEqual(report.mode, topology.MODE_STRUCTURAL)
        self.assertFalse(report.operational_ready)

    def test_checked_in_example_is_refused_for_operational_use(self) -> None:
        report = topology.validate_operational(
            EXAMPLE_PATH.read_bytes(),
            self._context(local_alias="node-alpha", selected_aliases=frozenset({"node-alpha"})),
        )
        self.assertEqual(report.codes(), ("operational_endpoint_reserved_example",))
        self.assertFalse(report.operational_ready)

    def test_fully_supplied_obligations_pass(self) -> None:
        report = topology.validate_operational(_payload(_document()), self._context())
        self.assertEqual(report.findings, ())
        self.assertTrue(report.operational_ready)
        self.assertEqual(report.mode, topology.MODE_OPERATIONAL)

    def test_trusted_local_identity_is_required_and_must_be_in_the_inventory(self) -> None:
        self.assertEqual(
            self._codes(_document(), local_alias=""),
            ("operational_local_identity_required",),
        )
        self.assertEqual(
            self._codes(_document(), local_alias="absent-node"),
            ("operational_local_identity_absent",),
        )

    def test_an_explicit_non_empty_subset_is_required(self) -> None:
        self.assertEqual(
            self._codes(_document(), selected_aliases=frozenset()),
            ("operational_subset_required",),
        )

    def test_absent_or_disabled_selected_nodes_are_refused(self) -> None:
        self.assertEqual(
            self._codes(_document(), selected_aliases=frozenset({"absent-node"})),
            ("operational_selected_alias_absent",),
        )
        self.assertEqual(
            self._codes(_document(_node(enabled=False))), ("operational_node_disabled",)
        )

    def test_a_role_alone_never_selects_a_node(self) -> None:
        broker = _node(alias="broker-node", roles=["a2a-broker"], keyRef=None, endpoint=None)
        del broker["keyRef"]
        del broker["endpoint"]
        report = topology.validate_operational(
            _payload(_document(_node(), broker)),
            self._context(selected_aliases=frozenset({"fixture-node"})),
        )
        self.assertTrue(report.operational_ready)
        # The broker was never selected, so nothing about it was validated or used.
        self.assertEqual(
            [finding.location for finding in report.notices], []
        )

    def test_missing_transport_policy_refuses_instead_of_approving(self) -> None:
        self.assertEqual(
            self._codes(_document(), endpoint_policy=None),
            ("operational_transport_policy_required",),
        )

    def test_consumer_transport_policy_decides_the_destination(self) -> None:
        self.assertEqual(
            self._codes(_document(), endpoint_policy=lambda facts: False),
            ("operational_endpoint_policy_rejected",),
        )
        # A truthy non-True result is not an approval.
        self.assertEqual(
            self._codes(_document(), endpoint_policy=lambda facts: 1),
            ("operational_endpoint_policy_rejected",),
        )

    def test_key_ref_cannot_authorize_itself(self) -> None:
        self.assertEqual(
            self._codes(_document(), keyring_resolver=None),
            ("operational_keyring_authorization_required",),
        )
        self.assertEqual(
            self._codes(_document(), keyring_resolver=lambda key_ref: False),
            ("operational_key_reference_unauthorized",),
        )

    def test_absent_optional_capabilities_are_notices_not_guesses(self) -> None:
        node = _node()
        del node["endpoint"]
        del node["keyRef"]
        report = topology.validate_operational(
            _payload(_document(node)), self._context(endpoint_policy=None, keyring_resolver=None)
        )
        self.assertTrue(report.operational_ready)
        self.assertEqual(
            {finding.code for finding in report.notices},
            {"notice_endpoint_absent", "notice_key_reference_absent"},
        )

    def test_unmodeled_routing_is_refused_rather_than_inferred(self) -> None:
        topology.require_modeled_capability("node-membership")
        for capability in ("relay-selection", "team-membership", "wiki-mapping",
                           "keyring-location", "broker-election"):
            with self.subTest(capability=capability):
                with self.assertRaises(topology.TopologyError) as caught:
                    topology.require_modeled_capability(capability)
                self.assertEqual(caught.exception.finding.code, "capability_not_modeled")


class DiagnosticContentTests(unittest.TestCase):
    def _all_text(self, report: topology.Report) -> str:
        return " ".join(
            str(finding) for finding in (*report.findings, *report.notices)
        )

    def test_public_diagnostics_never_echo_descriptor_content(self) -> None:
        node = _node(
            alias=SENTINEL_ALIAS,
            endpoint=SENTINEL_ENDPOINT,
            repoRoot=SENTINEL_REPO_ROOT,
            keyRef=SENTINEL_KEY_REF,
        )
        node["sentinel-unknown-key"] = "sentinel-value"
        documents = [
            _document(node),
            _document(_node(alias=SENTINEL_ALIAS, endpoint="https://SENTINEL.internal",
                            repoRoot=SENTINEL_REPO_ROOT, keyRef=SENTINEL_KEY_REF)),
            _document(_node(alias=SENTINEL_ALIAS, keyRef="worker:sentinel-other:g1:v1",
                            endpoint=SENTINEL_ENDPOINT, repoRoot=SENTINEL_REPO_ROOT)),
        ]
        secrets = (
            SENTINEL_ALIAS, "sentinel-endpoint", SENTINEL_REPO_ROOT, "sentinel-unknown-key",
            "sentinel-value", "sentinel-other", "SENTINEL",
        )
        for index, document in enumerate(documents):
            for report in (
                topology.validate_structural(_payload(document)),
                topology.validate_operational(
                    _payload(document),
                    topology.OperationalContext(
                        local_alias=SENTINEL_ALIAS,
                        selected_aliases=frozenset({SENTINEL_ALIAS}),
                    ),
                ),
            ):
                text = self._all_text(report)
                with self.subTest(document=index, mode=report.mode):
                    self.assertNotEqual(text, "")
                    for secret in secrets:
                        self.assertNotIn(secret, text)

    def test_descriptor_diagnostics_never_echo_the_path(self) -> None:
        with _fixture_root() as root:
            code = _read_code(root, "sentinel-descriptor-name.json")
            self.assertEqual(code, "descriptor_missing")
            with topology.trusted_root(root) as root_fd:
                report = topology.read_and_validate_structural(
                    root_fd, "sentinel-descriptor-name.json", _trust()
                )
            text = self._all_text(report)
            self.assertNotIn("sentinel-descriptor-name", text)
            self.assertNotIn(str(root), text)


class BoundaryTests(unittest.TestCase):
    """The declared boundary of this slice, asserted against the module's AST.

    Substring scans would be fooled by prose in a docstring or comment, so the
    checks below read the parsed module instead.
    """

    TREE = ast.parse(Path(topology.__file__).read_text(encoding="utf-8"))

    #: Everything the validator is allowed to depend on: stdlib only, no
    #: packaging, no process/network/write surface.
    ALLOWED_IMPORTS = frozenset(
        {
            "__future__", "contextlib", "dataclasses", "errno", "functools",
            "ipaddress", "json", "os", "pathlib", "re", "stat", "typing",
            "urllib.parse",
        }
    )

    def _imports(self) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        return names

    def test_module_depends_only_on_the_declared_stdlib_surface(self) -> None:
        self.assertEqual(self._imports() - self.ALLOWED_IMPORTS, set())

    def test_module_does_not_import_bridge_packaging(self) -> None:
        # bridge/core/prestop_json.py and bridge/utils/secure_fs.py were read
        # as precedents; importing them (directly or via ccc_secure_fs, which
        # loads the bridge module through importlib) is out of scope here.
        for forbidden in ("bridge", "ccc_secure_fs", "prestop_json", "importlib",
                          "importlib.util"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self._imports())

    def test_module_is_not_an_installed_or_executable_cli(self) -> None:
        guards = [
            node
            for node in ast.walk(self.TREE)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "__name__"
        ]
        self.assertEqual(guards, [])
        self.assertFalse(os.access(topology.__file__, os.X_OK))
        self.assertIsNone(getattr(topology, "main", None))

    def test_module_uses_no_write_or_mutating_filesystem_calls(self) -> None:
        attributes = {
            node.attr for node in ast.walk(self.TREE) if isinstance(node, ast.Attribute)
        }
        for forbidden in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "write",
                          "remove", "unlink", "rename", "replace", "mkdir",
                          "chmod", "chown", "truncate", "symlink", "system"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, attributes)

    def test_schema_and_example_are_the_checked_in_artifacts(self) -> None:
        self.assertTrue(topology.SCHEMA_PATH.is_file())
        self.assertTrue(EXAMPLE_PATH.is_file())
        schema = json.loads(topology.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["version"]["const"], 1)


if __name__ == "__main__":
    unittest.main()
