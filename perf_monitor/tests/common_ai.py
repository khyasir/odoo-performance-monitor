# -*- coding: utf-8 -*-
"""Fake Bedrock runtime client for the AI tests: no network, scripted Converse answers. When botocore is
installed, every request is validated against the real Converse input shape."""
import copy
from contextlib import contextmanager
from unittest.mock import patch

_SHAPE = []


def _validate(kwargs):
    try:
        import botocore.session  # pylint: disable=import-outside-toplevel
        from botocore.validate import ParamValidator  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    if not _SHAPE:
        model = botocore.session.get_session().get_service_model('bedrock-runtime')
        _SHAPE.append(model.operation_model('Converse').input_shape)
    report = ParamValidator().validate(kwargs, _SHAPE[0])
    if report.has_errors():
        raise AssertionError('invalid Converse request: %s' % report.generate_report())


def answer(text, tin=1000, tout=200, stop='end_turn'):
    return {'output': {'message': {'role': 'assistant', 'content': [{'text': text}]}}, 'stopReason': stop,
            'usage': {'inputTokens': tin, 'outputTokens': tout}}


def tool_use(uses, text='', tin=1000, tout=100):
    """``uses``: list of (name, input) the model asks for in one turn."""
    content = [{'text': text}] if text else []
    for n, (name, args) in enumerate(uses):
        content.append({'toolUse': {'toolUseId': 'tool-%d-%s' % (n, name), 'name': name, 'input': args}})
    return {'output': {'message': {'role': 'assistant', 'content': content}}, 'stopReason': 'tool_use',
            'usage': {'inputTokens': tin, 'outputTokens': tout}}


class FakeBedrock(object):
    """``script``: list of responses (the last one repeats) or callable(request, n) -> response / exception."""

    def __init__(self, script):
        self.script = script
        self.requests = []

    def converse(self, **kwargs):
        _validate(kwargs)
        self.requests.append(copy.deepcopy(kwargs))
        n = len(self.requests)
        step = self.script(kwargs, n) if callable(self.script) else self.script[min(n, len(self.script)) - 1]
        if isinstance(step, Exception):
            raise step
        return copy.deepcopy(step)


@contextmanager
def fake_bedrock(env, script):
    """Patch perf.issue._bedrock_client: yields the FakeBedrock (and records the regions asked)."""
    client = FakeBedrock(script)
    client.regions = []

    def _client(issue, region, **kwargs):
        client.regions.append(region)
        client.client_kwargs = kwargs
        return client
    with patch.object(type(env['perf.issue']), '_bedrock_client', _client):
        yield client
