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


def interim_record(*blocks):
    return {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'toolUse',
            'content': list(blocks)}}


def call_block():
    return {'type': 'toolCall', 'id': 'call-1', 'name': 'bash', 'arguments': {'command': 'PRIVATE_COMMAND'}}


def test_interim_only_forwards_bounded_redacted_assistant_text():
    d = decoder()
    value = interim_record({'type': 'thinking', 'thinking': 'PRIVATE_REASONING'},
                           {'type': 'text', 'text': 'Checking api_key=' + 'x'*80}, call_block())
    events = d.feed(json.dumps(value))
    assert [event.kind for event in events] == ['text_delta', 'message_completed']
    assert 'Checking' in events[0].text
    assert 'x'*20 not in repr(events) and 'PRIVATE' not in repr(events)
    assert d.feed(json.dumps(interim_record(call_block()))) is None
    long = d.feed(json.dumps(interim_record({'type': 'text', 'text': 'a'*6000}, call_block())))
    assert len(long[0].text) == 4096
    assert d.final is None


@pytest.mark.parametrize('blocks', [[], [{'type': 'text', 'text': 'not a tool response'}],
    [None, call_block()], [{'type': 'text', 'text': 4}, call_block()],
    [{**call_block(), 'arguments': 'PRIVATE'}], [{**call_block(), 'id': ''}],
    [{**call_block(), 'name': None}]])
def test_malformed_interim_is_not_delivered(blocks):
    with pytest.raises(ValueError):
        decoder().feed(json.dumps(interim_record(*blocks)))


@pytest.mark.anyio
async def test_fragmented_interim_frames_preserve_event_order_and_final_once():
    queue = asyncio.Queue()
    reader = asyncio.StreamReader()
    task = asyncio.create_task(p.read_progress(reader, queue))
    frames = [{'type': 'session', 'version': 3},
              interim_record({'type': 'text', 'text': '검증 중'}, call_block()), frame(),
              frame(phase='settled', success=True),
              {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop',
               'content': [{'type': 'text', 'text': 'done'}]}}]
    wire = ('\n'.join(map(json.dumps, frames))+'\n').encode()
    for byte in wire:
        reader.feed_data(bytes([byte]))
    reader.feed_eof()
    d = await task
    assert [queue.get_nowait().kind for _ in range(queue.qsize())] == [
        'text_delta', 'message_completed', 'tool_started', 'tool_completed']
    assert d.finish() == b'done'
