# -*- coding: utf-8 -*-
import json

from odoo import api

from .. import runtime
from ..tools.probe import RateLimiter


def set_agent_params(env, **params):
    for key, value in params.items():
        env['ir.config_parameter'].sudo().set_param('perf_monitor_agent.%s' % key, value)
    runtime.clear_config_cache()


def reset_limiter():
    runtime._limiter = RateLimiter(runtime.MAX_ROWS_PER_MINUTE)


def json_rpc(case, route, params, headers=None):
    """POST a JSON-RPC call through the test HTTP server (Odoo 16 has no make_jsonrpc_request)."""
    resp = case.url_open(route, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': params}),
                         headers=dict(headers or {}, **{'Content-Type': 'application/json'}))
    case.assertEqual(resp.status_code, 200, resp.text)
    return resp.json()


@api.model
def probe_app_name(self):
    """Patched onto res.partner by the tests: what PostgreSQL sees as application_name right now."""
    self.env.cr.execute('SHOW application_name')
    return self.env.cr.fetchone()[0]
