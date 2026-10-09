# -*- coding: utf-8 -*-
import json
import logging
import re
import threading
import urllib.parse
from datetime import timedelta

import requests

from odoo import api, fields, models, release

from .. import runtime
from ..tools.ui_context import summary as ui_summary_text

_logger = logging.getLogger(__name__)

PUSH_PATH = '/perf_monitor/api/v1/push_slow_calls'
PUSH_BATCH = 200  # central accepts up to 500 per push
PUSH_MAX_BATCHES = 5  # per cron run (central throttles pushes per minute)
PUSH_TIMEOUT = (5, 30)  # connect, read seconds
DEFAULT_RETENTION_DAYS = 7
MAX_ROWS = 50000  # hard cap of the local table, enforced by the cleanup cron
KINDS = [('rpc', 'RPC'), ('http', 'HTTP'), ('cron', 'Cron'), ('tile', 'Tile')]
# module data defaults (data/ir_config_parameter.xml): set on install only when the key is missing, then
# overridable in Settings > Perf Monitor Agent (the token is never in code / data)
PARAM_DEFAULTS = ('central_url', 'central_db')
_LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost')
_RX_URL = re.compile(r'^https?://[A-Za-z0-9.\-:\[\]]+(/[A-Za-z0-9._\-/]*)?$')
_RX_DB = re.compile(r'^[A-Za-z0-9_.\-]+$')


def is_allowed_central_url(url):
    """https:// anywhere, plain http:// only to a loopback host (same rule as perf_monitor)."""
    if not url or not _RX_URL.match(url):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == 'https':
        return bool(parts.hostname)
    return parts.hostname in _LOOPBACK_HOSTS


class PerfAgentSlowCall(models.Model):
    """Slow HTTP requests / RPC calls / cron runs of this DB (written by runtime.record_slow_call with
    raw SQL on a separate cursor: keep the columns in sync with runtime._INSERT_COLS)."""
    _name = 'perf.agent.slow.call'
    _description = 'Perf Monitor Agent: Slow Call'
    _order = 'ts desc, id desc'
    _rec_name = 'ts'

    ts = fields.Datetime('Started at', required=True, index=True, readonly=True)
    duration_ms = fields.Float('Duration (ms)', digits=(16, 1), readonly=True, group_operator='max')
    kind = fields.Selection(KINDS, required=True, default='http', readonly=True)
    uid = fields.Integer('User ID', readonly=True)
    login = fields.Char(readonly=True)
    model_name = fields.Char('Model', readonly=True)
    method = fields.Char(readonly=True)
    route = fields.Char(readonly=True)
    cron_name = fields.Char('Cron', readonly=True)
    app_name = fields.Char('application_name', readonly=True)
    sql_count = fields.Integer('SQL queries', readonly=True)
    sql_ms = fields.Float('SQL time (ms)', digits=(16, 1), readonly=True)
    error = fields.Char('Exception', readonly=True)
    top_sql = fields.Text('Top SQL (literals stripped)', readonly=True)
    top_sql_ms = fields.Float('Top SQL total (ms)', digits=(16, 1), readonly=True)
    top_sql_calls = fields.Integer('Top SQL executions', readonly=True)
    top_sql_fingerprint = fields.Char('SQL fingerprint', readonly=True)
    top_sql_callsite = fields.Char('Python call site', readonly=True)
    pushed = fields.Boolean(readonly=True, index=True)
    # UI context (where the user was: X-Perf-UI header of the web client, see tools/ui_context.py)
    ui_dashboard = fields.Char('Dashboard', readonly=True)
    ui_dashboard_model = fields.Char('Dashboard model', readonly=True)
    ui_dashboard_id = fields.Integer('Dashboard ID', readonly=True)
    ui_action = fields.Char('Action', readonly=True, help='Action name (or xmlid / client tag when unresolved)')
    ui_action_id = fields.Integer('Action ID', readonly=True)
    ui_menu = fields.Char('Menu', readonly=True)
    ui_menu_id = fields.Integer('Menu ID', readonly=True)
    ui_model = fields.Char('Screen model', readonly=True)
    ui_record_id = fields.Integer('Record ID', readonly=True)
    ui_view_type = fields.Char('View type', readonly=True)
    # dashboard tile / widget (odoo_dashboards_saas data.analytics.saas): the tile of a per-tile RPC, or the
    # slowest tile of a call computing many (each of them then has its own 'tile' row, parent_id = the call)
    ui_tile = fields.Char('Tile', readonly=True, help='Dashboard tile / widget name')
    ui_tile_id = fields.Integer('Tile ID', readonly=True)
    ui_tile_type = fields.Char('Tile type', readonly=True)
    tile_ms = fields.Float('Tile time (ms)', digits=(16, 1), readonly=True, group_operator='max',
                           help='Time spent computing this tile (the slowest tile of a multi-tile call)')
    tile_count = fields.Integer('Tiles computed', readonly=True)
    user_name = fields.Char('User', readonly=True)
    parent_id = fields.Many2one('perf.agent.slow.call', 'Call', readonly=True, ondelete='cascade', index=True,
                                help='Slow call this tile row belongs to')
    tile_ids = fields.One2many('perf.agent.slow.call', 'parent_id', 'Tiles', readonly=True)
    ui_summary = fields.Char('Summary', compute='_compute_ui_summary',
                             help='"Dashboard X > Tile Y (menu Z) by user U at <local time> took N s" '
                                  '(in your time zone)')

    @api.depends('ts', 'duration_ms', 'ui_dashboard', 'ui_action', 'ui_menu', 'ui_model', 'ui_record_id',
                 'ui_tile', 'login')
    @api.depends_context('tz')
    def _compute_ui_summary(self):
        for rec in self:
            when = fields.Datetime.context_timestamp(rec, rec.ts).strftime('%Y-%m-%d %H:%M') if rec.ts else '?'
            rec.ui_summary = ui_summary_text(
                when, rec.duration_ms, dashboard=rec.ui_dashboard, action=rec.ui_action, menu=rec.ui_menu,
                model=rec.ui_model, record_id=rec.ui_record_id, cron_name=rec.cron_name,
                model_name=rec.model_name, method=rec.method, route=rec.route, tile=rec.ui_tile,
                login=rec.login)

    def init(self):
        super().init()
        # the push cron only ever looks at the unpushed rows
        self.env.cr.execute('CREATE INDEX IF NOT EXISTS perf_agent_slow_call_unpushed_idx '
                            'ON perf_agent_slow_call (id) WHERE NOT pushed')

    def _register_hook(self):
        super()._register_hook()
        try:
            wrapped = runtime.install_tile_timers(self.env)
            if wrapped:
                _logger.info('perf_monitor_agent: timing odoo_dashboards_saas tiles (%s callables)', wrapped)
        except Exception:
            _logger.warning('perf_monitor_agent: cannot install the dashboard tile timers', exc_info=True)

    @api.model
    def _set_default_params(self, defaults):
        """Module data (install): set perf_monitor_agent.<key> for the keys not set yet (a value set by
        the deploy script / Settings is kept). Returns the keys set."""
        params = self.env['ir.config_parameter'].sudo()
        done = []
        for key, value in (defaults or {}).items():
            if key in PARAM_DEFAULTS and value and not params.get_param('perf_monitor_agent.%s' % key):
                params.set_param('perf_monitor_agent.%s' % key, value)
                done.append(key)
        return done

    # ------------------------------------------------------------------ cleanup
    @api.model
    def _cron_cleanup(self):
        """Delete rows older than perf_monitor_agent.retention_days (default 7), pushed or not, and
        keep at most MAX_ROWS rows. Returns the number of deleted rows."""
        try:
            days = int(self.env['ir.config_parameter'].sudo().get_param(
                'perf_monitor_agent.retention_days', DEFAULT_RETENTION_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        cutoff = fields.Datetime.now() - timedelta(days=max(days, 1))
        self.flush_model()
        cr = self.env.cr
        cr.execute('DELETE FROM perf_agent_slow_call WHERE ts < %s OR ts > %s',
                   [cutoff, fields.Datetime.now() + timedelta(days=1)])
        deleted = cr.rowcount
        cr.execute('DELETE FROM perf_agent_slow_call WHERE id <= ('
                   'SELECT id FROM perf_agent_slow_call ORDER BY id DESC OFFSET %s LIMIT 1)', [MAX_ROWS])
        deleted += cr.rowcount
        self.invalidate_model()
        _logger.info('perf_monitor_agent: cleanup deleted %s slow call rows (retention %s days)', deleted, days)
        return deleted

    # ------------------------------------------------------------------ push
    def _push_payload(self):
        return [{
            'id': r.id, 'ts': fields.Datetime.to_string(r.ts), 'duration_ms': r.duration_ms, 'kind': r.kind,
            'uid': r.uid, 'login': r.login or '', 'model_name': r.model_name or '', 'method': r.method or '',
            'route': r.route or '', 'cron_name': r.cron_name or '', 'app_name': r.app_name or '',
            'sql_count': r.sql_count, 'sql_ms': r.sql_ms, 'error': r.error or '', 'top_sql': r.top_sql or '',
            'top_sql_ms': r.top_sql_ms, 'top_sql_calls': r.top_sql_calls,
            'top_sql_fingerprint': r.top_sql_fingerprint or '', 'top_sql_callsite': r.top_sql_callsite or '',
            'ui_dashboard': r.ui_dashboard or '', 'ui_dashboard_model': r.ui_dashboard_model or '',
            'ui_dashboard_id': r.ui_dashboard_id or None, 'ui_action': r.ui_action or '',
            'ui_action_id': r.ui_action_id or None, 'ui_menu': r.ui_menu or '', 'ui_menu_id': r.ui_menu_id or None,
            'ui_model': r.ui_model or '', 'ui_record_id': r.ui_record_id or None,
            'ui_view_type': r.ui_view_type or '',
            'ui_tile': r.ui_tile or '', 'ui_tile_id': r.ui_tile_id or None, 'ui_tile_type': r.ui_tile_type or '',
            'tile_ms': r.tile_ms, 'tile_count': r.tile_count, 'user_name': r.user_name or '',
            'parent_id': r.parent_id.id or None,
        } for r in self]

    @api.model
    def _push_config(self):
        params = self.env['ir.config_parameter'].sudo()
        url = (params.get_param('perf_monitor_agent.central_url') or '').strip().rstrip('/')
        token = (params.get_param('perf_monitor_agent.central_token') or '').strip()
        db = (params.get_param('perf_monitor_agent.central_db') or '').strip()
        return url, token, db

    @api.model
    def _valid_push_config(self, what='push'):
        """(url, token, central_db) when the central endpoint is usable, else None (reason logged, never
        the token). Shared by the slow calls push and the collector ("Perf Monitor: agent collect")."""
        url, token, central_db = self._push_config()
        if not url or not token:
            _logger.info('perf_monitor_agent: %s skipped, perf_monitor_agent.central_url / '
                         'central_token not set', what)
            return None
        if not is_allowed_central_url(url):
            _logger.warning('perf_monitor_agent: %s skipped, central_url must be https:// (plain http:// '
                            'only to 127.0.0.1, [::1] or localhost): %s', what, url)
            return None
        if central_db and not _RX_DB.match(central_db):
            _logger.warning('perf_monitor_agent: %s skipped, invalid central_db', what)
            return None
        return url, token, central_db

    @api.model
    def _cron_push(self):
        """Send unpushed rows to the central perf_monitor in batches; a row is marked pushed only
        after the central answered 200 {"ok": true} (it de-duplicates re-sent rows). Network /
        central errors are logged (never the token) and the rows are retried at the next run.
        Returns the number of rows pushed."""
        push_cfg = self._valid_push_config()
        if not push_cfg:
            return 0
        url, token, central_db = push_cfg
        testing = getattr(threading.current_thread(), 'testing', False)
        pushed = 0
        with requests.Session() as session:
            session.headers['User-Agent'] = 'perf_monitor_agent/%s' % release.version
            if central_db:
                try:  # pin the central DB in the session cookie (multi-DB central), like the collector
                    session.get('%s/web?%s' % (url, urllib.parse.urlencode({'db': central_db})),
                                timeout=PUSH_TIMEOUT, allow_redirects=False)
                except requests.RequestException as exc:
                    _logger.warning('perf_monitor_agent: central unreachable: %s', exc)
                    return 0
            for _batch in range(PUSH_MAX_BATCHES):
                rows = self.search([('pushed', '=', False)], order='id', limit=PUSH_BATCH)
                if not rows:
                    break
                body = json.dumps({'version': 1, 'db_name': self.env.cr.dbname,
                                   'agent_version': self.env.ref('base.module_perf_monitor_agent').latest_version
                                   or '', 'calls': rows._push_payload()})
                try:
                    resp = session.post(url + PUSH_PATH, data=body, timeout=PUSH_TIMEOUT, allow_redirects=False,
                                        headers={'Content-Type': 'application/json', 'X-Perf-Token': token})
                    result = resp.json() if resp.status_code == 200 else None
                except (requests.RequestException, ValueError) as exc:
                    _logger.warning('perf_monitor_agent: push failed: %s', exc)
                    break
                if not isinstance(result, dict) or not result.get('ok'):
                    _logger.warning('perf_monitor_agent: push refused by central: HTTP %s %s',
                                    resp.status_code, (resp.text or '')[:200])
                    break
                rows.write({'pushed': True})
                pushed += len(rows)
                _logger.info('perf_monitor_agent: pushed %s slow calls (central: %s)', len(rows),
                             {k: result.get(k) for k in ('accepted', 'rejected', 'duplicates')})
                if not testing:
                    self.env.cr.commit()  # a later failing batch must not un-mark this one
        return pushed
