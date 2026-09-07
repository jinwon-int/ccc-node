"""Bounded Danso JSONL progress decoding; never forwards transcript bodies."""
from __future__ import annotations

import json
import uuid

from telegram_bot.core.agent_runtime import ToolCompletedEvent, ToolStartedEvent

LINE_CAP = 2 * 1024 * 1024
STREAM_CAP = 32 * 1024 * 1024
TOOLS = {'read', 'write', 'edit', 'bash', 'other'}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate progress key')
        result[key] = value
    return result


class ProgressDecoder:
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.sequence = 0
        self.active = None
        self.header = False
        self.final = None

    def feed(self, line):
        record = json.loads(line, object_pairs_hook=_object)
        if not isinstance(record, dict) or self.final is not None:
            raise ValueError('invalid progress stream')
        kind = record.get('type')
        if not self.header:
            if kind != 'session' or type(record.get('version')) is not int or record['version'] != 3:
                raise ValueError('missing progress session header')
            self.header = True
            return None
        if kind == 'danso_progress':
            return self._progress(record)
        if kind == 'custom' and record.get('customType') == 'danso.compaction.v1':
            if self.active is not None:
                raise ValueError('compaction during tool execution')
            return None
        if kind != 'message' or not isinstance(record.get('message'), dict):
            raise ValueError('invalid transcript frame')
        message = record['message']
        if message.get('role') == 'assistant' and message.get('stopReason') == 'stop':
            if self.active is not None or not isinstance(message.get('content'), list):
                raise ValueError('invalid final response')
            parts = []
            for block in message['content']:
                if not isinstance(block, dict):
                    raise ValueError('invalid final block')
                if block.get('type') == 'text':
                    if not isinstance(block.get('text'), str):
                        raise ValueError('invalid final text')
                    parts.append(block['text'])
            self.final = '\n'.join(parts).strip()
        return None

    def _progress(self, record):
        phase = record.get('phase')
        keys = {'type', 'version', 'sequence', 'phase', 'tool'}
        if phase == 'settled':
            keys.add('success')
        if (set(record) != keys or type(record.get('version')) is not int or record['version'] != 1
                or type(record.get('sequence')) is not int
                or not isinstance(record.get('tool'), str) or record['tool'] not in TOOLS):
            raise ValueError('invalid progress record')
        seq, tool = record['sequence'], record['tool']
        if phase == 'started':
            if self.active is not None or seq != self.sequence + 1:
                raise ValueError('invalid progress order')
            self.sequence = seq
            self.active = tool
            return ToolStartedEvent(tool_call_id=f'{self.run_id}:{seq}', tool_name=tool, arguments={})
        if phase == 'settled':
            if self.active != tool or seq != self.sequence or type(record['success']) is not bool:
                raise ValueError('invalid progress settlement')
            self.active = None
            return ToolCompletedEvent(tool_call_id=f'{self.run_id}:{seq}', tool_name=tool,
                                      result={'settled': True}, success=record['success'])
        raise ValueError('unsupported progress phase')

    def finish(self):
        if self.active is not None or not self.final:
            raise ValueError('incomplete progress stream')
        return self.final.encode('utf-8')


async def read_progress(stream, queue):
    decoder = ProgressDecoder()
    pending = bytearray()
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if total > STREAM_CAP:
            raise ValueError('progress stream limit')
        pending.extend(chunk)
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending = bytearray(rest)
            if len(line) > LINE_CAP:
                raise ValueError('progress line limit')
            event = decoder.feed(line)
            if event is not None:
                await queue.put(event)
        if len(pending) > LINE_CAP:
            raise ValueError('progress line limit')
    if pending:
        raise ValueError('truncated progress frame')
    return decoder
