# -*- coding: utf-8 -*-
"""Collector-facing endpoints.

auth='none' routes of a non server-wide module only resolve once a database is
selected: the collector pins it with GET /web?db=<db> + a cookie jar (see DEPLOY.md).
The token is accepted ONLY in the X-Perf-Token header (query strings end up in access logs).
"""
import json
import logging
import os
import re
import threading
import time

import psycopg2

from odoo import SUPERUSER_ID, http
from odoo.http import request

from ..models.perf_common import get_int_param
from ..models.perf_redact import is_allowed_central_url

_logger = logging.getLogger(__name__)

COLLECTOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'collector')
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_SLOW_CALLS_BODY_BYTES = 2 * 1024 * 1024  # 500 calls x (2000 chars SQL + fields)
_SAFE_DB = re.compile(r'^[A-Za-z0-9_.\-]+$')
_SAFE_TOKEN = re.compile(r'^[A-Za-z0-9_\-]{20,128}$')

# Per-server push throttle (fixed one-minute window). The dict lives in the worker process, so with
# N Odoo workers the effective limit is up to N x perf_monitor.max_pushes_per_minute; put nginx
# limit_req in front for a global limit (see DEPLOY.md).
_THROTTLE = {}
_THROTTLE_LOCK = threading.Lock()
DEFAULT_MAX_PUSHES_PER_MINUTE = 10


def _throttled(dbname, server_id, limit, now=None):
    """Count one push for (dbname, server_id); True when the current minute already had ``limit``."""
    if limit <= 0:
        return False
    minute = int((now if now is not None else time.time()) // 60)
    key = (dbname, server_id)
    with _THROTTLE_LOCK:
        if len(_THROTTLE) > 10000:  # drop stale windows, keeps the dict bounded
            for k in [k for k, v in _THROTTLE.items() if v[0] != minute]:
                del _THROTTLE[k]
        window = _THROTTLE.get(key)
        if not window or window[0] != minute:
            window = _THROTTLE[key] = [minute, 0]
        window[1] += 1
        return window[1] > limit


def _read_collector_file(name):
    with open(os.path.join(COLLECTOR_DIR, name), 'rb') as fh:
        return fh.read()


def collector_version():
    match = re.search(rb'^__version__ = "([^"]+)"', _read_collector_file('perf_collector.py'), re.M)
    return match.group(1).decode() if match else 'unknown'


def configured_central_url(env):
    """Central URL rendered into install.sh: ir.config_parameter perf_monitor.central_url (set by the
    Add Server wizard), only if it passes is_allowed_central_url(), else None (the route answers 400).
    Never derived from the request (Host header / url_root are attacker controlled), and no fallback
    to web.base.url (Odoo rewrites it from the Host header of every admin login)."""
    url = (env['ir.config_parameter'].sudo().get_param('perf_monitor.central_url') or '').strip().rstrip('/')
    return url if url and is_allowed_central_url(url) else None


class PerfMonitorController(http.Controller):

    def _su_env(self):
        return request.env(user=SUPERUSER_ID, su=True)

    def _request_token(self):
        return request.httprequest.headers.get('X-Perf-Token')

    def _server_from_token(self, token):
        if not token or not _SAFE_TOKEN.match(token):
            return None
        server = self._su_env()['perf.server']._find_by_token(token)
        return server or None

    def _text(self, body, status=200, content_type='text/plain; charset=utf-8'):
        return request.make_response(body, headers=[('Content-Type', content_type),
                                                    ('Cache-Control', 'no-store')], status=status)

    @http.route('/perf_monitor/collector/install.sh', type='http', auth='none', methods=['GET'],
                csrf=False, save_session=False)
    def install_sh(self, **kw):
        token = self._request_token()
        if not self._server_from_token(token):
            return self._text('invalid token\n', 401)
        central_url = configured_central_url(self._su_env())
        if not central_url:
            return self._text('central URL not configured: set the system parameter perf_monitor.central_url '
                              '(Add Server wizard, or Settings > Technical > System Parameters) to an https:// '
                              'URL (http:// only for 127.0.0.1, [::1] or localhost)\n', 400)
        central_db = request.env.cr.dbname or ''
        if not _SAFE_DB.match(central_db):
            return self._text('cannot determine central db\n', 400)
        script = _read_collector_file('install.sh.tmpl').decode()
        for key, value in (('@@CENTRAL_URL@@', central_url), ('@@CENTRAL_DB@@', central_db),
                           ('@@TOKEN@@', token), ('@@COLLECTOR_VERSION@@', collector_version()),
                           ('@@SYSTEMD_UNIT@@', _read_collector_file('perf-monitor-collector.service').decode())):
            script = script.replace(key, value)
        return self._text(script, content_type='text/x-shellscript; charset=utf-8')

    @http.route('/perf_monitor/collector/perf_collector.py', type='http', auth='none', methods=['GET'],
                csrf=False, save_session=False)
    def collector_py(self, **kw):
        if not self._server_from_token(self._request_token()):
            return self._text('invalid token\n', 401)
        return self._text(_read_collector_file('perf_collector.py'), content_type='text/x-python; charset=utf-8')

    @http.route('/perf_monitor/api/v1/push', type='http', auth='none', methods=['POST'],
                csrf=False, save_session=False)
    def push(self, **kw):
        def reply(data, status=200, headers=None):
            return request.make_json_response(data, status=status, headers=headers)

        server = self._server_from_token(self._request_token())
        if not server:
            return reply({'ok': False, 'error': 'invalid token'}, 401)
        limit = get_int_param(self._su_env(), 'perf_monitor.max_pushes_per_minute', DEFAULT_MAX_PUSHES_PER_MINUTE)
        if _throttled(request.env.cr.dbname, server.id, limit):
            return reply({'ok': False, 'error': 'too many pushes'}, 429, headers=[('Retry-After', '60')])
        length = request.httprequest.content_length
        if length is None:
            return reply({'ok': False, 'error': 'content-length required'}, 411)
        if length > MAX_BODY_BYTES:
            return reply({'ok': False, 'error': 'payload too large'}, 413)
        try:
            payload = json.loads(request.httprequest.get_data(cache=False)[:MAX_BODY_BYTES + 1] or b'null')
            result = server._ingest_payload(payload)
        # ValueError: JSONDecodeError; RecursionError: deeply nested JSON
        except (ValueError, OverflowError, TypeError, RecursionError, psycopg2.DataError) as exc:
            request.env.cr.rollback()
            _logger.info('perf_monitor: payload from server %s refused: %s', server.id, exc)
            return reply({'ok': False, 'error': 'bad payload: %s' % str(exc)[:200]}, 400)
        return reply(dict(result, ok=True))

    @http.route('/perf_monitor/api/v1/push_slow_calls', type='http', auth='none', methods=['POST'],
                csrf=False, save_session=False)
    def push_slow_calls(self, **kw):
        """Batch of slow requests / crons recorded by perf_monitor_agent in a monitored DB
        (same token scheme and per-server throttle as /perf_monitor/api/v1/push)."""
        def reply(data, status=200, headers=None):
            return request.make_json_response(data, status=status, headers=headers)

        server = self._server_from_token(self._request_token())
        if not server:
            return reply({'ok': False, 'error': 'invalid token'}, 401)
        limit = get_int_param(self._su_env(), 'perf_monitor.max_pushes_per_minute', DEFAULT_MAX_PUSHES_PER_MINUTE)
        if _throttled(request.env.cr.dbname, 'slow_calls:%s' % server.id, limit):
            return reply({'ok': False, 'error': 'too many pushes'}, 429, headers=[('Retry-After', '60')])
        length = request.httprequest.content_length
        if length is None:
            return reply({'ok': False, 'error': 'content-length required'}, 411)
        if length > MAX_SLOW_CALLS_BODY_BYTES:
            return reply({'ok': False, 'error': 'payload too large'}, 413)
        try:
            payload = json.loads(request.httprequest.get_data(cache=False)[:MAX_SLOW_CALLS_BODY_BYTES + 1] or b'null')
            result = self._su_env()['perf.agent.slow.call.remote']._ingest_slow_calls(server, payload)
            server._touch_last_push()  # also with 0 rows: the agent is alive
        except (ValueError, OverflowError, TypeError, RecursionError, psycopg2.DataError) as exc:
            request.env.cr.rollback()
            _logger.info('perf_monitor: slow calls from server %s refused: %s', server.id, exc)
            return reply({'ok': False, 'error': 'bad payload: %s' % str(exc)[:200]}, 400)
        if result.get('accepted'):
            # issue rules only (no AI call here); never fails the push. Staging runs without cron threads.
            self._su_env()['perf.issue']._build_issues_safe()
        return reply(dict(result, ok=True))
