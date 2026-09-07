"""Progress framing and privacy boundaries, without executing a provider."""
import asyncio
import json

import pytest

from telegram_bot.core import danso_progress as p


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def frame(**changes):
    return {'type':'danso_progress','version':1,'sequence':1,'phase':'started','tool':'bash',**changes}


def decoder():
    value = p.ProgressDecoder()
    value.feed(json.dumps({'type':'session','version':3}))
    return value


@pytest.mark.parametrize('changes', [
    {'version':True}, {'sequence':True}, {'sequence':2}, {'tool':'SECRET'},
    {'phase':'settled','success':True}, {'arguments':{'secret':'PRIVATE'}},
])
def test_invalid_or_body_bearing_progress_is_rejected(changes):
    with pytest.raises(ValueError):
        decoder().feed(json.dumps(frame(**changes)))


def test_order_unique_run_ids_and_no_transcript_body_forwarding():
    a, b = decoder(), decoder()
    first = a.feed(json.dumps(frame()))
    assert first.tool_call_id != b.feed(json.dumps(frame())).tool_call_id
    with pytest.raises(ValueError):
        a.feed(json.dumps(frame(sequence=2)))
    assert a.feed(json.dumps({'type':'message','message':{'role':'toolResult',
      'content':[{'type':'text','text':'PRIVATE_TOOL_BODY'}]}})) is None
    result = a.feed(json.dumps(frame(phase='settled',success=False)))
    assert result.tool_call_id == first.tool_call_id and not result.success
    assert 'PRIVATE' not in repr(result)
    assert a.feed(json.dumps({'type':'message','message':{'role':'assistant','stopReason':'stop',
        'content':[{'type':'text','text':'final'}]}})) is None
    assert a.finish() == b'final'
    with pytest.raises(ValueError):
        a.feed(json.dumps(frame(sequence=2)))


@pytest.mark.parametrize('change', [{'sequence':2}, {'tool':'read'}, {'success':1}])
def test_mismatched_settlement_rejected(change):
    d = decoder()
    d.feed(json.dumps(frame()))
    with pytest.raises(ValueError):
        d.feed(json.dumps(frame(**{'phase':'settled','success':True,**change})))


def test_missing_header_duplicate_keys_and_unsettled_final_are_rejected():
    with pytest.raises(ValueError):
        p.ProgressDecoder().feed(json.dumps(frame()))
    with pytest.raises(ValueError):
        decoder().feed('{"type":"message","type":"session"}')
    d=decoder()
    d.feed(json.dumps(frame()))
    with pytest.raises(ValueError):
        d.finish()
    with pytest.raises(ValueError):
        d.feed(json.dumps({'type':'message','message':{'role':'assistant','stopReason':'stop','content':[]}}))


@pytest.mark.anyio
async def test_limits_and_partial_frames_fail_closed(monkeypatch):
    monkeypatch.setattr(p,'LINE_CAP',64)
    for data in (b'{' * 65, b'{"type":"session","version":3}'):
        reader=asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        with pytest.raises(ValueError):
            await p.read_progress(reader,asyncio.Queue())
    monkeypatch.setattr(p,'STREAM_CAP',10)
    reader=asyncio.StreamReader()
    reader.feed_data(b'x'*11)
    reader.feed_eof()
    with pytest.raises(ValueError):
        await p.read_progress(reader,asyncio.Queue())
