# -*- coding: utf-8 -*-
import re
from datetime import timedelta

from odoo import api, fields, models

from .perf_ai_text import real_callsite
from .perf_common import create_server_ts_index, get_int_param, purge_older_than
from .perf_redact import redact_sql
from .perf_server import _to_dt, _to_float, _to_int, _to_str
from .sql_fingerprint import sql_fingerprint

MAX_SLOW_CALLS = 500
_RX_SHA1 = re.compile(r'^[0-9a-f]{40}$')
KINDS = [('rpc', 'RPC'), ('http', 'HTTP'), ('cron', 'Cron'), ('tile', 'Tile')]
UI_INTS = ('ui_dashboard_id', 'ui_action_id', 'ui_menu_id', 'ui_record_id', 'ui_tile_id', 'tile_count')
UI_STRS = (('ui_dashboard', 256), ('ui_dashboard_model', 128), ('ui_action', 256), ('ui_menu', 256),
           ('ui_model', 128), ('ui_view_type', 32), ('ui_tile', 256), ('ui_tile_type', 32), ('user_name', 256))


def ui_summary(when, duration_ms, dashboard=None, action=None, menu=None, model=None, record_id=None,
               cron_name=None, model_name=None, method=None, route=None, db_name=None, tile=None, login=None):
    """One line for a task: "Dashboard 'Sales' > Tile 'Top Products' (menu Sales/Dashboards) by user ali at
    2026-09-27 16:05 took 4.2 s (model.method) [db]" (same text as perf_monitor_agent/tools/ui_context.summary,
    plus the source DB)."""
    took = '%.1f s' % ((duration_ms or 0.0) / 1000.0)
    call = '.'.join(p for p in (model_name, method) if p) or route or ''
    if dashboard:
        text = "Dashboard '%s'" % dashboard + (" > Tile '%s'" % tile if tile else '')
        text += ' (menu %s)' % menu if menu else ''
    elif action:
        text = "'%s' opened" % action + (" (menu '%s')" % menu if menu else '')
    elif menu:
        text = "Menu '%s' opened" % menu
    elif cron_name:
        text, call = "Cron '%s' ran" % cron_name, ''
    else:
        text, call = '%s called' % (call or 'request'), ''
    if tile and not dashboard:
        text += " > Tile '%s'" % tile
    if model and record_id and not dashboard:
        text += ' on %s #%s' % (model, record_id)
    if login and not cron_name:
        text += ' by user %s' % login
    text += ' at %s took %s' % (when, took)
    return text + (' (%s)' % call if call else '') + (' [%s]' % db_name if db_name else '')


class PerfAgentSlowCallRemote(models.Model):
    """Slow requests / crons recorded by perf_monitor_agent in a monitored DB and pushed here
    (POST /perf_monitor/api/v1/push_slow_calls). Same fields as perf.agent.slow.call of the agent."""
    _name = 'perf.agent.slow.call.remote'
    _description = 'Agent Slow Call (pushed by perf_monitor_agent)'
    _order = 'ts desc, duration_ms desc'
    _rec_name = 'ts'

    server_id = fields.Many2one('perf.server', required=True, ondelete='cascade', index=True)
    agent_id = fields.Integer('Agent row ID', help='id of the row in the monitored DB (dedup of re-sent pushes)')
    ts = fields.Datetime('Started at', required=True, index=True)
    duration_ms = fields.Float('Duration (ms)', digits=(16, 1), group_operator='max')
    db_name = fields.Char('Database')
    uid = fields.Integer('User ID')
    login = fields.Char('Login')
    kind = fields.Selection(KINDS, required=True, default='http')
    model_name = fields.Char('Model')
    method = fields.Char('Method')
    route = fields.Char('Route')
    cron_name = fields.Char('Cron')
    sql_count = fields.Integer('SQL queries')
    sql_ms = fields.Float('SQL time (ms)', digits=(16, 1))
    top_sql = fields.Text('Slowest SQL (redacted)')
    top_sql_ms = fields.Float('Slowest SQL (ms)', digits=(16, 1))
    top_sql_fingerprint = fields.Char('SQL fingerprint', index=True)
    top_sql_callsite = fields.Char('Python call site')
    top_sql_calls = fields.Integer('Top SQL executions')
    app_name = fields.Char('application_name')
    error = fields.Char('Exception')
    # UI context sent by perf_monitor_agent >= 16.0.1.1.0 (the screen the user was on)
    ui_dashboard = fields.Char('Dashboard', index=True)
    ui_dashboard_model = fields.Char('Dashboard model')
    ui_dashboard_id = fields.Integer('Dashboard ID')
    ui_action = fields.Char('Action', help='Action name (or xmlid / client tag when unresolved)')
    ui_action_id = fields.Integer('Action ID')
    ui_menu = fields.Char('Menu')
    ui_menu_id = fields.Integer('Menu ID')
    ui_model = fields.Char('Screen model')
    ui_record_id = fields.Integer('Record ID')
    ui_view_type = fields.Char('View type')
    # dashboard tile / widget and user name, sent by perf_monitor_agent >= 16.0.1.2.0
    ui_tile = fields.Char('Tile', index=True, help='Dashboard tile / widget name')
    ui_tile_id = fields.Integer('Tile ID')
    ui_tile_type = fields.Char('Tile type')
    tile_ms = fields.Float('Tile time (ms)', digits=(16, 1), group_operator='max',
                           help='Time spent computing this tile (the slowest tile of a multi-tile call)')
    tile_count = fields.Integer('Tiles computed')
    user_name = fields.Char('User')
    parent_agent_id = fields.Integer('Call agent row ID', help='agent id of the call a tile row belongs to')
    parent_id = fields.Many2one('perf.agent.slow.call.remote', 'Call', ondelete='cascade', index=True)
    tile_ids = fields.One2many('perf.agent.slow.call.remote', 'parent_id', 'Tiles')
    # the UI shows seconds; the *_ms columns stay the stored values (pushed by the agent, rules, sorting)
    duration_s = fields.Float('Duration (s)', digits=(16, 2), compute='_compute_seconds')
    sql_s = fields.Float('SQL time (s)', digits=(16, 2), compute='_compute_seconds')
    top_sql_s = fields.Float('Slowest SQL (s)', digits=(16, 2), compute='_compute_seconds')
    tile_s = fields.Float('Tile time (s)', digits=(16, 2), compute='_compute_seconds')
    ui_summary = fields.Char('Summary', compute='_compute_ui_summary',
                             help='"Dashboard X > Tile Y (menu Z) by user U at <local time> took N s" '
                                  '(in your time zone)')

    @api.depends('duration_ms', 'sql_ms', 'top_sql_ms', 'tile_ms')
    def _compute_seconds(self):
        for rec in self:
            rec.duration_s = (rec.duration_ms or 0.0) / 1000.0
            rec.sql_s = (rec.sql_ms or 0.0) / 1000.0
            rec.top_sql_s = (rec.top_sql_ms or 0.0) / 1000.0
            rec.tile_s = (rec.tile_ms or 0.0) / 1000.0

    @api.depends('ts', 'duration_ms', 'ui_dashboard', 'ui_action', 'ui_menu', 'ui_model', 'ui_record_id',
                 'ui_tile', 'login')
    @api.depends_context('tz')
    def _compute_ui_summary(self):
        for rec in self:
            when = fields.Datetime.context_timestamp(rec, rec.ts).strftime('%Y-%m-%d %H:%M') if rec.ts else '?'
            rec.ui_summary = ui_summary(
                when, rec.duration_ms, dashboard=rec.ui_dashboard, action=rec.ui_action, menu=rec.ui_menu,
                model=rec.ui_model, record_id=rec.ui_record_id, cron_name=rec.cron_name,
                model_name=rec.model_name, method=rec.method, route=rec.route,
                db_name=rec.db_name, tile=rec.ui_tile, login=rec.login)

    def init(self):
        super().init()
        create_server_ts_index(self.env.cr, self._table)

    @api.model
    def _cron_purge(self):
        days = get_int_param(self.env, 'perf_monitor.slow_call_retention_days', 14)
        return purge_older_than(self, days)

    @api.model
    def _link_parents(self, server, db_name, tiles):
        """Tile rows -> the row of their call (same server / DB; the agent pushes a call before its tiles)."""
        if not tiles:
            return
        parents = self.search([('server_id', '=', server.id), ('db_name', '=', db_name),
                               ('agent_id', 'in', list(set(tiles.mapped('parent_agent_id'))))])
        by_agent_id = {p.agent_id: p.id for p in parents}
        for tile in tiles:
            if by_agent_id.get(tile.parent_agent_id):
                tile.parent_id = by_agent_id[tile.parent_agent_id]

    @api.model
    def _ingest_slow_calls(self, server, payload):
        """Validate / clamp an agent push and store the new rows. Returns {'accepted', 'rejected'}.
        Rows already stored (same server / db_name / agent id: a re-sent batch) are counted as
        duplicates. Raises ValueError on a structurally invalid payload (nothing is written)."""
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('unsupported payload version')
        calls = payload.get('calls') or []
        if not isinstance(calls, list) or not all(isinstance(c, dict) for c in calls):
            raise ValueError('calls must be a list of objects')
        if len(calls) > MAX_SLOW_CALLS:
            raise ValueError('calls batch too large (%d > %d)' % (len(calls), MAX_SLOW_CALLS))
        db_name = _to_str(payload.get('db_name') or '', 64) or False
        now = fields.Datetime.now()
        days = get_int_param(self.env, 'perf_monitor.slow_call_retention_days', 14)
        oldest, newest = now - timedelta(days=days), now + timedelta(minutes=5)
        kinds = dict(KINDS)
        rows, rejected = [], 0
        for item in calls:
            ts = _to_dt(item.get('ts'))
            try:
                agent_id = _to_int(item.get('id'), 'id')
                uid = _to_int(item.get('uid'), 'uid')
                sql_count = _to_int(item.get('sql_count'), 'sql_count')
                top_sql_calls = _to_int(item.get('top_sql_calls'), 'top_sql_calls')
                ui_ints = {fname: _to_int(item.get(fname), fname) or False for fname in UI_INTS}
                parent_agent_id = _to_int(item.get('parent_id'), 'parent_id')
            except ValueError:  # out-of-range int: only this row is refused
                rejected += 1
                continue
            if not ts or not oldest <= ts <= newest or agent_id is None:
                rejected += 1
                continue
            top_sql = redact_sql(_to_str(item.get('top_sql') or '', 2000)) or False
            fingerprint = _to_str(item.get('top_sql_fingerprint') or '', 40)
            if not fingerprint or not _RX_SHA1.match(fingerprint):
                fingerprint = sql_fingerprint(top_sql) if top_sql else False
            vals = {
                'server_id': server.id, 'agent_id': agent_id, 'ts': ts, 'db_name': db_name,
                'uid': uid or False, 'sql_count': sql_count or 0, 'top_sql_calls': top_sql_calls or 0,
                'top_sql': top_sql,
                'top_sql_fingerprint': fingerprint or False,
                'kind': item.get('kind') if item.get('kind') in kinds else 'http',
            }
            for fname in ('duration_ms', 'sql_ms', 'top_sql_ms', 'tile_ms'):
                num = _to_float(item.get(fname))
                vals[fname] = min(max(num, 0.0), 1e12) if num is not None else 0.0
            for fname, size in (('login', 128), ('model_name', 128), ('method', 128), ('route', 256),
                                ('cron_name', 256), ('app_name', 64), ('error', 128)) + UI_STRS:
                vals[fname] = _to_str(item.get(fname), size) or False
            # several frames sent: keep the first application frame (wags_logger's cursor patch / SQL layer
            # skipped) BEFORE clipping to 256 characters (the real frame may come after a few skipped ones); a
            # lone skipped frame is kept as evidence (the issue never uses it as its call site)
            callsite = _to_str(item.get('top_sql_callsite'), 4000) or ''
            vals['top_sql_callsite'] = _to_str(real_callsite(callsite) or callsite, 256) or False
            vals.update(ui_ints, parent_agent_id=parent_agent_id if parent_agent_id and parent_agent_id > 0 else False)
            rows.append(vals)
        existing = set()
        if rows:
            recs = self.search_read([('server_id', '=', server.id), ('db_name', '=', db_name),
                                     ('agent_id', 'in', [r['agent_id'] for r in rows])], ['agent_id'])
            existing = {r['agent_id'] for r in recs}
        new = []
        for vals in rows:
            if vals['agent_id'] not in existing:
                existing.add(vals['agent_id'])
                new.append(vals)
        created = self.create(new)
        self._link_parents(server, db_name, created.filtered('parent_agent_id'))
        self.flush_model()
        return {'accepted': len(new), 'rejected': rejected, 'duplicates': len(rows) - len(new)}
