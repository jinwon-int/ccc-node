"""Syntax profile tests, not schema, authority or lifecycle integration tests."""

import json
import traceback

import pytest

from telegram_bot.core.prestop_json import (
    MAX_BYTES,
    MAX_DEPTH,
    MAX_INTEGER,
    MIN_INTEGER,
    PrestopJSONError,
    decode_object,
)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, MIN_INTEGER, MAX_INTEGER,
                                    "", "한글", "😀", [], {}, [True, None, {"x": 1}]])
def test_supported_values(value):
    assert decode_object(json.dumps({"value": value}).encode()) == {"value": value}


@pytest.mark.parametrize("data", [
    b'', b' ', b'[]', b'null', b'true', b'1', b'"text"',
    b'{', b'}', b'{]', b'{"x":}', b'{"x":01}', b'{"x":+1}',
    b'{"x":1,}', b'{}{}', b'{} trailing', b'{}\x00',
    b'{/*comment*/}', b'{"x":"\x01"}', b'\xff', b'\xc0\xaf',
    b'\xef\xbb\xbf{}', b'{"x":"\xed\xa0\x80"}',
    b'{"x":1.0}', b'{"x":1e0}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":-Infinity}', b'{"x":9223372036854775808}',
    b'{"x":-9223372036854775809}', b'{"x":12345678901234567890}',
    b'{"x":1,"x":2}', b'{"x":1,"\\u0078":2}',
    b'{"nested":[{"x":1,"x":2}]}', b'{"\\ud800":1}',
    b'{"x":["\\udfff"]}', b'{"x":"\\ud800a"}',
])
def test_rejected_syntax(data):
    with pytest.raises(PrestopJSONError):
        decode_object(data)


@pytest.mark.parametrize("data", [None, "{}", bytearray(b'{}'), memoryview(b'{}'), 1])
def test_exact_bytes_required(data):
    with pytest.raises(PrestopJSONError, match="Expected bytes"):
        decode_object(data)


def test_bytes_subclass_rejected():
    class Derived(bytes):
        pass

    with pytest.raises(PrestopJSONError, match="Expected bytes"):
        decode_object(Derived(b'{}'))


def test_byte_boundary_and_multibyte_accounting():
    assert decode_object(b'{}' + b' ' * (MAX_BYTES - 2)) == {}
    with pytest.raises(PrestopJSONError, match="byte limit"):
        decode_object(b'{}' + b' ' * (MAX_BYTES - 1))
    data = json.dumps({"x": "한" * 6000}, ensure_ascii=False).encode()
    with pytest.raises(PrestopJSONError, match="byte limit"):
        decode_object(data)


@pytest.mark.parametrize("kind", ["array", "object"])
def test_depth_boundary(kind):
    def nested(levels):
        value = 0
        for _ in range(levels - 1):
            value = [value] if kind == "array" else {"x": value}
        return json.dumps({"root": value}).encode()

    decode_object(nested(MAX_DEPTH))
    with pytest.raises(PrestopJSONError, match="nesting limit"):
        decode_object(nested(MAX_DEPTH + 1))


def test_deep_input_rejected_before_recursive_decoder(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Recursive decoder must not receive excessive nesting")

    monkeypatch.setattr(json, "loads", forbidden)
    with pytest.raises(PrestopJSONError, match="nesting limit"):
        decode_object(b'{"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}')


def test_brackets_and_escaped_quotes_in_strings():
    value = '[{]}"\\' * 100
    assert decode_object(json.dumps({"x": value}).encode()) == {"x": value}
    assert decode_object(b'{"x":"\\ud83d\\ude00"}') == {"x": "😀"}


def test_huge_integer_rejected():
    with pytest.raises(PrestopJSONError, match="integer out of range"):
        decode_object(b'{"x":' + b'9' * 10000 + b'}')


def test_whitespace_negative_zero_and_distinct_unicode_keys():
    assert decode_object(b' \r\n\t{"x":-0}\t') == {"x": 0}
    assert decode_object('{"é":1,"é":2}'.encode()) == {"é": 1, "é": 2}


@pytest.mark.parametrize("data", [
    b'{"PRIVATE_SENTINEL":}', b'{"PRIVATE_SENTINEL":"\xff"}',
    b'{"PRIVATE_SENTINEL":NaN}', b'{"PRIVATE_SENTINEL":1,"PRIVATE_SENTINEL":2}',
    b'{"PRIVATE_SENTINEL":"\\ud800"}',
])
def test_error_messages_and_default_traceback_omit_input(data):
    with pytest.raises(PrestopJSONError) as caught:
        decode_object(data)
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert "PRIVATE_SENTINEL" not in "".join(traceback.format_exception(caught.value))


def test_unknown_fields_and_bool_remain_schema_responsibility():
    result = decode_object(b'{"unknown_version":true,"unauthenticated":null}')
    assert result == {"unknown_version": True, "unauthenticated": None}
    assert type(result["unknown_version"]) is bool
