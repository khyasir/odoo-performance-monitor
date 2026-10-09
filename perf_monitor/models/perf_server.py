# -*- coding: utf-8 -*-
import hashlib
import hmac
import logging
import math
import re
import secrets
import shlex
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models, release
from odoo.exceptions import UserError, ValidationError

from .perf_common import COLLECTOR_CONF_PATH, get_int_param
from .perf_metric import METRIC_FIELDS
from .perf_redact import is_allowed_central_url, redact_sql

_logger = logging.getLogger(__name__)

RIGHTSIZE_MIN_DAYS = 14
RIGHTSIZE_WINDOW_DAYS = 28
STATUS_GREEN_MIN = 5
STATUS_YELLOW_MIN = 10
# DB agent sources (perf_monitor_agent, slow calls only): status from last_push
AGENT_STATUS_GREEN_H = 24
AGENT_STATUS_YELLOW_D = 7
AGENT_LAST_PUSH_MIN_WRITE_S = 60  # last_push is rewritten at most once a minute (no write storm)
AGENT_HOST_PREFIX = 'agent:'
# collector_version sent by the in-agent collector (perf_monitor_agent "Perf Monitor: agent collect", every
# 5 minutes instead of every minute): its "Last seen" status gets wider windows
AGENT_COLLECTOR_PREFIX = 'perf_monitor_agent'
AGENT_COLLECT_GREEN_MIN = 11
AGENT_COLLECT_YELLOW_MIN = 20
# name suffixes of DB agent cards stripped before matching a server card by name (host_server_id)
AGENT_NAME_SUFFIXES = (' (agent)', ' (perf_monitor_agent)')
SOURCE_KINDS = [('collector', 'Server (collector)'), ('agent', 'DB agent (slow calls only)')]

# Hard caps for one push (the controller also caps the raw body size).
MAX_BATCH = {'metrics': 5000, 'sql': 1000, 'live': 5000}

# cumulative pg_stat_statements counters easily pass 2**31: stored as Float(digits=(20, 0)) (numeric)
SQL_COUNTER_FIELDS = ('calls', 'rows', 'shared_blks_hit', 'shared_blks_read', 'temp_blks_read',
                      'temp_blks_written')
COUNTER_MAX = 10 ** 18
INT4_MAX = 2 ** 31 - 1
FUTURE_TOLERANCE_MIN = 5
_RX_FRACTION = re.compile(r'\.([0-9]+)')  # fromisoformat (3.10) wants 3 or 6 digits
RETENTION_PARAMS = {  # model -> (ir.config_parameter, default days); same params as the purge crons
    'perf.metric': ('perf_monitor.metric_retention_days', 35),
    'perf.sql.snapshot': ('perf_monitor.sql_retention_days', 14),
    'perf.live.query': ('perf_monitor.live_retention_days', 3),
}
DEDUP_KEYS = {'perf.metric': ('ts',), 'perf.sql.snapshot': ('ts', 'queryid'), 'perf.live.query': ('ts', 'pid')}
SQL_FLOAT_FIELDS = ('total_ms', 'mean_ms', 'max_ms', 'cum_mean_ms')
LIVE_CHAR_FIELDS = ('usename', 'application_name', 'client_addr', 'state', 'wait_event_type', 'wait_event',
                    'blocking_pids')
# pg_stat_statements on the monitored database (payload key "pgss", sent by perf_monitor_agent >= 16.0.1.4.0 and
# perf_collector.py >= 1.0.2; older senders: unknown). Nothing is ever installed by perf_monitor or the agent.
PGSS_STATUSES = [('unknown', 'Unknown'), ('ok', 'Top SQL: ON'), ('missing', 'Top SQL OFF - pg_stat_statements missing'),
                 ('unreadable', 'Top SQL OFF - pg_stat_statements not readable')]
PGSS_HELP = ('pg_stat_statements on the monitored database, reported with every metrics push. Missing: add '
             'pg_stat_statements to shared_preload_libraries (AWS RDS: DB parameter group; restart if it was not '
             'there), then as the master user / superuser run CREATE EXTENSION IF NOT EXISTS pg_stat_statements; '
             'in the monitored database. Not readable: usually not in shared_preload_libraries yet (restart '
             'needed) or no read right. Unknown: an older agent / collector that does not report it. See the '
             'README; nothing is installed automatically.')
HOST_FIELDS = {  # payload key -> (field, max length)
    'instance_type': 'instance_type', 'instance_id': 'instance_id', 'region': 'region', 'az': 'az',
    'pg_version': 'pg_version', 'odoo_version': 'odoo_version',
}


def hash_token(token):
    return hashlib.sha256((token or '').encode()).hexdigest()


def status_from_last_seen(last_seen, now, green_min=STATUS_GREEN_MIN, yellow_min=STATUS_YELLOW_MIN):
    if not last_seen:
        return 'unknown'
    age = now - last_seen
    if age < timedelta(minutes=green_min):
        return 'green'
    if age < timedelta(minutes=yellow_min):
        return 'yellow'
    return 'red'


def status_from_last_push(last_push, now):
    """Status of a DB agent source: green < 24 h, yellow < 7 days, unknown otherwise (or never pushed)."""
    if not last_push:
        return 'unknown'
    age = now - last_push
    if age < timedelta(hours=AGENT_STATUS_GREEN_H):
        return 'green'
    if age < timedelta(days=AGENT_STATUS_YELLOW_D):
        return 'yellow'
    return 'unknown'


def source_kind_from_host(host):
    return 'agent' if (host or '').startswith(AGENT_HOST_PREFIX) else 'collector'


def name_key(name):
    """Comparison key of a server name: case and repeated / surrounding spaces do not matter
    ("ERP", "erp" and " erp  " are the same name)."""
    return ' '.join((name or '').split()).casefold()


def base_server_name(name):
    """Name of a DB agent card without its " (agent)" / " (perf_monitor_agent)" suffix."""
    name = (name or '').strip()
    for suffix in AGENT_NAME_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)].strip()
    return name


def _to_float(value):
    """float(value), or None when missing, not numeric or not finite (json accepts NaN/Infinity)."""
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return num if math.isfinite(num) else None


def _to_int(value, name, low=0, high=INT4_MAX):
    """int for an Integer (int4) column; None when missing. Raises ValueError when the value is
    non-finite or outside [low, high] (it would otherwise overflow the column: HTTP 500)."""
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(num) or not low <= num <= high:
        raise ValueError('%s out of range' % name)
    return int(num)


def _to_counter(value):
    """Non-negative finite counter clamped to COUNTER_MAX (0 when missing/invalid)."""
    num = _to_float(value)
    if num is None or num < 0:
        return 0.0
    return float(min(round(num), COUNTER_MAX))


def _in_window(ts, window):
    return bool(ts) and window[0] <= ts <= window[1]


def _to_dt(value):
    """Naive UTC datetime (seconds precision) from an ISO 8601 string, or None. A trailing Z or an
    offset such as +03:00 is converted to UTC; a string without offset is taken as UTC."""
    if not value or not isinstance(value, str) or len(value) > 64:
        return None
    text = value.strip()
    if text[-1:] in ('Z', 'z'):
        text = text[:-1] + '+00:00'
    text = _RX_FRACTION.sub(lambda m: '.' + (m.group(1) + '000000')[:6], text, count=1)
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None
    return dt.replace(microsecond=0)


def _to_str(value, size):
    """str(value) cut to ``size``; NUL characters are removed (PostgreSQL text cannot store them)."""
    if value is None:
        return False
    return str(value).replace('\x00', '')[:size]


class PerfServer(models.Model):
    _name = 'perf.server'
    _description = 'Monitored Server'
    _inherit = ['mail.thread']
    # 'collector' > 'agent': collector servers first, then the DB agent sources
    _order = 'source_kind desc, environment desc, name'

    name = fields.Char(required=True, tracking=True,
                       help='Unique: identifies the server card (the database name may repeat across hosts).')
    active = fields.Boolean(default=True, tracking=True)
    # NB: cannot be called `env` (would shadow the ORM Environment attribute)
    environment = fields.Selection([('staging', 'Staging'), ('live', 'Live')], string='Env', required=True,
                                   default='staging', tracking=True)
    host = fields.Char(help='Host name / IP of the monitored Odoo server', tracking=True)
    db_name = fields.Char('Database', tracking=True)
    source_kind = fields.Selection(
        SOURCE_KINDS, string='Source', compute='_compute_source_kind', store=True, readonly=False,
        required=True, index=True, precompute=True,
        help='Server (collector): OS / PostgreSQL metrics pushed by perf_collector.py. DB agent: '
             'perf_monitor_agent of a database, pushes slow calls only (host "agent:<db>"). Set from the '
             'host when it changes; can be overridden.')
    hostname = fields.Char('Reported hostname', readonly=True)
    instance_type = fields.Char(readonly=True)
    instance_id = fields.Char(readonly=True)
    region = fields.Char(readonly=True)
    az = fields.Char('Availability zone', readonly=True)
    vcpu = fields.Integer('vCPU', readonly=True)
    ram_mb = fields.Integer('RAM (MB)', readonly=True)
    odoo_version = fields.Char(readonly=True)
    pg_version = fields.Char('PostgreSQL version', readonly=True)
    status = fields.Selection([
        ('green', 'Green'), ('yellow', 'Yellow'), ('red', 'Red'), ('unknown', 'Unknown'),
    ], default='unknown', readonly=True, required=True,
        help='Server: from last_seen, green < 5 min, yellow < 10 min, red otherwise, unknown if never seen. '
             'DB agent: from last_push, green < 24 h, yellow < 7 days, unknown otherwise.')
    last_seen = fields.Datetime(readonly=True)
    last_push = fields.Datetime(readonly=True, help='Last accepted slow calls push (perf_monitor_agent), '
                                                    'also with 0 rows; updated at most once a minute')
    slow_calls_24h = fields.Integer('Slow calls 24h', compute='_compute_slow_call_stats',
                                    help='Slow calls (tile rows excluded) started in the last 24 hours')
    slow_calls_7d = fields.Integer('Slow calls 7d', compute='_compute_slow_call_stats')
    last_slow_call = fields.Datetime(compute='_compute_slow_call_stats')
    host_server_id = fields.Many2one(
        'perf.server', 'On server', compute='_compute_host_server', store=True, readonly=False,
        ondelete='set null', domain="[('source_kind', '=', 'collector'), ('id', '!=', id)]",
        help='Server card this DB agent source runs on. Match by Name; db_name may repeat: set automatically '
             'when a server card has exactly the name of this card (without a trailing " (agent)"), '
             'otherwise set it by hand.')
    collector_version = fields.Char(readonly=True)
    pgss_status = fields.Selection(PGSS_STATUSES, 'Top SQL (pg_stat_statements)', default='unknown',
                                   readonly=True, help=PGSS_HELP)
    pgss_checked_at = fields.Datetime('pg_stat_statements checked at', readonly=True,
                                      help='Last push that reported the pg_stat_statements status')
    token_hash = fields.Char(index=True, readonly=True, copy=False, groups='perf_monitor.group_perf_manager')
    token_hint = fields.Char(readonly=True, copy=False, help='Last 4 characters of the collector token')

    latest_metric_id = fields.Many2one('perf.metric', readonly=True, ondelete='set null')
    cpu_pct = fields.Float(related='latest_metric_id.cpu_pct', string='CPU %')
    ram_pct = fields.Float(related='latest_metric_id.ram_pct', string='RAM %')
    disk_pct = fields.Float(related='latest_metric_id.disk_pct', string='Disk %')
    load1 = fields.Float(related='latest_metric_id.load1', string='Load 1m')
    net_rx_kbps = fields.Float(related='latest_metric_id.net_rx_kbps', string='Net RX (kbit/s)')
    net_tx_kbps = fields.Float(related='latest_metric_id.net_tx_kbps', string='Net TX (kbit/s)')
    db_latency_ms = fields.Float(related='latest_metric_id.db_latency_ms', string='DB latency (ms)')
    metric_ts = fields.Datetime(related='latest_metric_id.ts', string='Latest sample')

    metric_days = fields.Integer(compute='_compute_metric_days', help='Distinct calendar days with metrics')
    rightsize_recommendation = fields.Text('Right-size', readonly=True,
                                           default='insufficient data: 0/%d days' % RIGHTSIZE_MIN_DAYS)
    rightsize_date = fields.Datetime('Right-size computed at', readonly=True)

    recent_metric_ids = fields.Many2many('perf.metric', compute='_compute_recent', string='Latest metrics')
    recent_sql_ids = fields.Many2many('perf.sql.snapshot', compute='_compute_recent', string='Top SQL')
    recent_live_ids = fields.Many2many('perf.live.query', compute='_compute_recent', string='Live queries')
    metric_count = fields.Integer(compute='_compute_counts')
    sql_count = fields.Integer(compute='_compute_counts')
    live_count = fields.Integer(compute='_compute_counts')

    # ------------------------------------------------------------------ computes
    def _real_ids(self):
        return tuple(i for i in self.ids if isinstance(i, int))

    @api.depends('host')
    def _compute_source_kind(self):
        for server in self:
            server.source_kind = source_kind_from_host(server.host)

    def _compute_slow_call_stats(self):
        """One grouped query for the whole recordset (kanban / list read many servers at once)."""
        stats = {}
        ids = self._real_ids()
        if ids:
            self.env['perf.agent.slow.call.remote'].flush_model(['server_id', 'ts', 'kind'])
            now = fields.Datetime.now()
            self.env.cr.execute("""
                SELECT server_id,
                       COUNT(*) FILTER (WHERE kind <> 'tile' AND ts >= %s),
                       COUNT(*) FILTER (WHERE kind <> 'tile' AND ts >= %s),
                       MAX(ts)
                  FROM perf_agent_slow_call_remote
                 WHERE server_id IN %s
                 GROUP BY server_id
            """, (now - timedelta(hours=24), now - timedelta(days=7), ids))
            stats = {row[0]: row[1:] for row in self.env.cr.fetchall()}
        for server in self:
            day, week, last = stats.get(server.id, (0, 0, False))
            server.slow_calls_24h, server.slow_calls_7d, server.last_slow_call = day, week, last

    @api.depends('name', 'source_kind')
    def _compute_host_server(self):
        """Match by Name, never by db_name (several hosts can serve a database with the same name):
        an agent card is linked to the active server card named exactly like it, once a trailing
        " (agent)" is stripped. Without such a card the current (hand-set) link is kept."""
        agents = self.filtered(lambda s: s.source_kind == 'agent')
        by_name = {}
        keys = {name_key(base_server_name(n)) for n in agents.mapped('name')} - {''}
        if keys:
            for server in self.search([('source_kind', '=', 'collector')]):
                if name_key(server.name) in keys:
                    by_name.setdefault(name_key(server.name), server)
        for server in self:
            if server.source_kind != 'agent':
                server.host_server_id = False
                continue
            match = by_name.get(name_key(base_server_name(server.name)))
            if match and match != server:
                server.host_server_id = match

    def _link_agents_by_name(self):
        """A server card created / renamed after its agent cards: link the unlinked ones named like it."""
        collectors = self.filtered(lambda s: s.source_kind == 'collector' and s.active)
        if not collectors:
            return
        by_name = {}
        for s in collectors:
            by_name.setdefault(name_key(s.name), s)
        for agent in self.search([('source_kind', '=', 'agent'), ('host_server_id', '=', False)]):
            match = by_name.get(name_key(base_server_name(agent.name)))
            if match:
                agent.host_server_id = match

    @api.constrains('name')
    def _check_unique_name(self):
        """Name identifies a server card (archived ones included); db_name may repeat."""
        for server in self:
            name = (server.name or '').strip()
            if not name:
                raise ValidationError(_('The server name is required.'))
            others = self.with_context(active_test=False).search([('id', '!=', server.id)])
            if any(name_key(o.name) == name_key(server.name) for o in others):
                raise ValidationError(_('A server named "%s" already exists: server names must be unique '
                                        '(the database name may repeat).', server.name))

    @api.model_create_multi
    def create(self, vals_list):
        servers = super().create(vals_list)
        servers._link_agents_by_name()
        return servers

    def write(self, vals):
        res = super().write(vals)
        if 'name' in vals or 'source_kind' in vals or 'host' in vals or 'active' in vals:
            self._link_agents_by_name()
        return res

    def _compute_metric_days(self):
        days = {}
        ids = self._real_ids()
        if ids:
            self.env['perf.metric'].flush_model(['server_id', 'ts'])
            self.env.cr.execute(
                'SELECT server_id, COUNT(DISTINCT ts::date) FROM perf_metric WHERE server_id IN %s GROUP BY server_id',
                (ids,))
            days = dict(self.env.cr.fetchall())
        for server in self:
            server.metric_days = days.get(server.id, 0)

    def _compute_recent(self):
        for server in self:
            if not isinstance(server.id, int):
                server.recent_metric_ids = server.recent_sql_ids = server.recent_live_ids = False
                continue
            dom = [('server_id', '=', server.id)]
            server.recent_metric_ids = self.env['perf.metric'].search(dom, limit=60)
            server.recent_sql_ids = self.env['perf.sql.snapshot'].search(dom, limit=40)
            server.recent_live_ids = self.env['perf.live.query'].search(dom, limit=40)

    def _compute_counts(self):
        counts = {}
        for model, fname in (('perf.metric', 'metric_count'), ('perf.sql.snapshot', 'sql_count'),
                             ('perf.live.query', 'live_count')):
            data = self.env[model]._read_group([('server_id', 'in', self._real_ids())], ['server_id'], ['server_id'])
            counts[fname] = {d['server_id'][0]: d['server_id_count'] for d in data}
        for server in self:
            for fname, values in counts.items():
                server[fname] = values.get(server.id, 0)

    # ------------------------------------------------------------------ status
    def _refresh_status(self):
        now = fields.Datetime.now()
        for server in self:
            if server.source_kind == 'agent':
                status = status_from_last_push(server.last_push, now)
            elif (server.collector_version or '').startswith(AGENT_COLLECTOR_PREFIX):
                status = status_from_last_seen(server.last_seen, now, AGENT_COLLECT_GREEN_MIN,
                                               AGENT_COLLECT_YELLOW_MIN)
            else:
                status = status_from_last_seen(server.last_seen, now)
            if server.status != status:
                server.status = status

    # ------------------------------------------------------------------ right-size (collector-only text; CloudWatch card in perf_rightsize.py)
    def _compute_rightsize_recommendation(self):
        """Deterministic right-size text per server: {server_id: text}."""
        result = {}
        self.env['perf.metric'].flush_model()
        cutoff = fields.Datetime.now() - timedelta(days=RIGHTSIZE_WINDOW_DAYS)
        for server in self:
            days = server.metric_days
            if days < RIGHTSIZE_MIN_DAYS:
                result[server.id] = 'insufficient data: %d/%d days' % (days, RIGHTSIZE_MIN_DAYS)
                continue
            self.env.cr.execute("""
                SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY cpu_pct), MAX(cpu_pct),
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY ram_pct), MAX(ram_pct)
                  FROM perf_metric
                 WHERE server_id = %s AND ts >= %s
            """, (server.id, cutoff))
            p95_cpu, peak_cpu, p95_ram, peak_ram = [v or 0.0 for v in self.env.cr.fetchone()]
            result[server.id] = (
                'p95 CPU %.1f%%, peak %.1f%%, p95 RAM %.1f%%, peak %.1f%% (collector only, CloudWatch card not computed)'
                % (p95_cpu, peak_cpu, p95_ram, peak_ram))
        return result

    def action_compute_rightsize(self):
        now = fields.Datetime.now()
        for server_id, text in self._compute_rightsize_recommendation().items():
            server = self.browse(server_id)
            if server.rightsize_recommendation != text:
                server.write({'rightsize_recommendation': text, 'rightsize_date': now})
        return True

    @api.model
    def _cron_refresh(self):
        servers = self.search([])
        servers._refresh_status()
        servers.filtered(lambda s: s.source_kind != 'agent').action_compute_rightsize()
        return True

    # ------------------------------------------------------------------ tokens
    def _set_new_token(self):
        """Generate a token, store only its sha256 + hint, return the plain token (shown once)."""
        self.ensure_one()
        token = secrets.token_urlsafe(32)
        self.sudo().write({'token_hash': hash_token(token), 'token_hint': token[-4:]})
        return token

    @api.model
    def _find_by_token(self, token):
        if not token or not isinstance(token, str) or len(token) > 256:
            return self.browse()
        digest = hash_token(token)
        server = self.sudo().with_context(active_test=True).search([('token_hash', '=', digest)], limit=1)
        if server and hmac.compare_digest(server.token_hash or '', digest):
            return server
        return self.browse()

    def _check_manager(self):
        if not self.env.user.has_group('perf_monitor.group_perf_manager'):
            raise UserError(_('Only Perf Monitor Managers can manage collector tokens.'))

    def action_regenerate_token(self):
        """Revokes the current token at once (the collector gets HTTP 401) and opens a wizard showing the
        new token once, with the collector.conf path and the restart command."""
        self.ensure_one()
        self._check_manager()
        wizard = self.env['perf.server.add.wizard'].create({
            'server_id': self.id, 'name': self.name, 'environment': self.environment, 'host': self.host,
            'db_name': self.db_name, 'mode': 'regenerate', 'source_kind': self.source_kind or 'collector',
        })
        token = self._set_new_token()
        self.message_post(body=_('Collector token regenerated by %s; the previous token is revoked. The '
                                 'collector gets HTTP 401 until the new token is in %s.',
                                 self.env.user.name, COLLECTOR_CONF_PATH))
        return wizard._show_new_token(token)

    def action_revoke_token(self):
        self._check_manager()
        for server in self:
            server.sudo().write({'token_hash': False, 'token_hint': False})
            server.message_post(body=_('Collector token revoked by %s.', self.env.user.name))
        return True

    @api.model
    def _build_install_command(self, central_url, token, pg_host='localhost', pg_db=None):
        """One-line installer. The first curl pins the central DB in a cookie jar (see DEPLOY.md)."""
        central_url = (central_url or '').rstrip('/')
        if not is_allowed_central_url(central_url):
            raise UserError(_('Invalid central URL %s: it must be https:// (plain http:// is only allowed for '
                              '127.0.0.1, [::1] or localhost).', central_url))
        central_db = self.env.cr.dbname
        # sslmode=require at least; for RDS prefer sslmode=verify-full sslrootcert=/path/to/global-bundle.pem
        dsn = 'host=%s dbname=%s user=perf_monitor_ro password=CHANGE_ME sslmode=require' % (
            pg_host or 'localhost', pg_db or central_db)
        pin_url = '%s/web?db=%s' % (central_url, central_db)
        install_url = '%s/perf_monitor/collector/install.sh?db=%s' % (central_url, central_db)
        curl = "curl --proto '=https' --tlsv1.2" if central_url.startswith('https://') else 'curl'
        # token sent as a header (not in the query string) so it never lands in access logs; no -L:
        # curl would forward the custom X-Perf-Token header to whatever host a redirect points at
        return ('J=$(mktemp) && %s -sS -o /dev/null -c "$J" %s && %s -fsS -b "$J" -H %s %s '
                '| sudo PERF_PG_DSN=%s bash; rm -f "$J"'
                % (curl, shlex.quote(pin_url), curl, shlex.quote('X-Perf-Token: %s' % token),
                   shlex.quote(install_url), shlex.quote(dsn)))

    @api.model
    def _deploy_register_local(self, name, host, db_name=None, env='staging'):
        """Used by deploy_perf_monitor.sh through `odoo-bin shell`: create-or-rotate the
        local server record and return the plain token (never stored). The card is found by its
        (unique) name first; host + db_name is only the fallback (db_name may repeat across hosts). Only
        collector (server) cards are considered: a DB agent card is never turned into a server card."""
        db_name = db_name or self.env.cr.dbname
        Server = self.sudo().with_context(active_test=False)
        server = Server.browse()
        if name:
            server = Server.search([('source_kind', '=', 'collector')]).filtered(
                lambda s: name_key(s.name) == name_key(name))[:1]
        if not server:
            server = Server.search([('host', '=', host), ('db_name', '=', db_name),
                                ('source_kind', '=', 'collector')], limit=1)
        if not server:
            server = self.sudo().create({'name': name, 'host': host, 'db_name': db_name, 'environment': env})
        vals = {'active': True, 'odoo_version': release.version}
        if host and server.host != host:
            vals['host'] = host
        if db_name and server.db_name != db_name:
            vals['db_name'] = db_name
        server.write(vals)
        token = server._set_new_token()
        server.message_post(body=_('Collector token issued by the deploy script.'))
        return token

    # ------------------------------------------------------------------ ingestion
    def _touch_last_push(self):
        """Accepted slow calls push (perf_monitor_agent), even with 0 rows: last_push, and the status of
        a DB agent source (the status cron may not run: max_cron_threads=0). Written at most once per
        AGENT_LAST_PUSH_MIN_WRITE_S; a collector server's last_seen / status are never touched."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.last_push and now - self.last_push < timedelta(seconds=AGENT_LAST_PUSH_MIN_WRITE_S):
            return False
        vals = {'last_push': now}
        if self.source_kind == 'agent':
            status = status_from_last_push(now, now)
            if self.status != status:
                vals['status'] = status
        self.write(vals)
        self.flush_recordset()
        return True

    def _ingest_payload(self, payload):
        """Store a collector payload. Returns {'accepted': n, 'rejected': m}.

        Rows whose ``ts`` is more than 5 minutes in the future or older than the retention of their
        table are skipped and counted as rejected (as are rows without a valid ts / key and rows with
        an out-of-range integer such as ``pid``). ``last_seen`` is not updated when every row was
        rejected. Raises ValueError on a structurally invalid payload or an out-of-range host value
        (vcpu, ram_mb); every row is validated before anything is written, so a refused payload
        leaves nothing behind."""
        self.ensure_one()
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('unsupported payload version')
        batches = {}
        for key, cap in MAX_BATCH.items():
            items = payload.get(key) or []
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise ValueError('%s must be a list of objects' % key)
            if len(items) > cap:
                raise ValueError('%s batch too large (%d > %d)' % (key, len(items), cap))
            batches[key] = items
        host = payload.get('host') or {}
        if not isinstance(host, dict):
            raise ValueError('host must be an object')

        now = fields.Datetime.now()
        vals = {}
        for key, fname in HOST_FIELDS.items():
            if host.get(key):
                vals[fname] = _to_str(host[key], 128 if fname != 'pg_version' else 256)
        if host.get('hostname'):
            vals['hostname'] = _to_str(host['hostname'], 255)
        for key in ('vcpu', 'ram_mb'):
            num = _to_int(host.get(key), 'host.' + key)
            if num is not None:
                vals[key] = num
        if payload.get('collector_version'):
            vals['collector_version'] = _to_str(payload['collector_version'], 32)
        # pg_stat_statements status: absent / invalid (older agent or collector) = unknown, never an error
        pgss = payload.get('pgss')
        pgss = pgss if isinstance(pgss, str) and pgss in dict(PGSS_STATUSES) else 'unknown'
        if pgss != self.pgss_status:
            vals['pgss_status'] = pgss
        if pgss != 'unknown':
            vals['pgss_checked_at'] = now

        prepared, rejected = [], 0
        for key, model, prepare in (('metrics', 'perf.metric', self._prepare_metrics),
                                    ('sql', 'perf.sql.snapshot', self._prepare_sql),
                                    ('live', 'perf.live.query', self._prepare_live)):
            rows, skipped = prepare(batches[key], self._ts_window(model, now))
            prepared.append((model, rows))
            rejected += skipped
        accepted = 0
        for model, rows in prepared:
            accepted += self._create_new(model, rows)
        if accepted or not rejected:
            vals['last_seen'] = now
        else:
            # every row refused (typically collector clock skew): do not turn the card green
            _logger.warning('perf_monitor: all %d rows from server %s rejected (ts out of window?); '
                            'last_seen not updated', rejected, self.id)

        latest = self.env['perf.metric'].search(
            [('server_id', '=', self.id), ('ts', '<=', now + timedelta(minutes=FUTURE_TOLERANCE_MIN))], limit=1)
        if latest and latest != self.latest_metric_id:
            vals['latest_metric_id'] = latest.id
        self.write(vals)
        self._refresh_status()
        # flush here, inside the controller's try block, so that a DB error is a 400, not a 500
        self.flush_recordset()
        return {'accepted': accepted, 'rejected': rejected}

    @api.model
    def _ts_window(self, model, now):
        """(oldest, newest) accepted sample timestamps for ``model``."""
        key, default = RETENTION_PARAMS[model]
        days = get_int_param(self.env, key, default)
        return now - timedelta(days=days), now + timedelta(minutes=FUTURE_TOLERANCE_MIN)

    def _existing_keys(self, model, key_fields, stamps):
        if not stamps:
            return set()
        recs = self.env[model].search_read(
            [('server_id', '=', self.id), ('ts', '>=', min(stamps)), ('ts', '<=', max(stamps))], key_fields)
        return {tuple(r[f] for f in key_fields) for r in recs}

    def _create_new(self, model, rows):
        """Create ``rows`` except those already stored (re-sent spooled samples); returns the count."""
        key_fields = DEDUP_KEYS[model]
        seen = self._existing_keys(model, list(key_fields), [r['ts'] for r in rows])
        new = []
        for r in rows:
            key = tuple(r[f] for f in key_fields)
            if key not in seen:
                seen.add(key)
                new.append(r)
        self.env[model].create(new)
        return len(new)

    def _prepare_metrics(self, items, window):
        rows, rejected = [], 0
        for item in items:
            ts = _to_dt(item.get('ts'))
            if not _in_window(ts, window):
                rejected += 1
                continue
            vals = {'server_id': self.id, 'ts': ts}
            try:
                for fname in METRIC_FIELDS:
                    num = (_to_int(item.get(fname), fname) if fname == 'pg_connections'
                           else _to_float(item.get(fname)))
                    if num is not None:
                        vals[fname] = num
            except ValueError:  # out-of-range int4: only this row is refused
                rejected += 1
                continue
            rows.append(vals)
        return rows, rejected

    def _prepare_sql(self, items, window):
        rows, rejected = [], 0
        for item in items:
            ts = _to_dt(item.get('ts'))
            if not _in_window(ts, window) or not item.get('queryid'):
                rejected += 1
                continue
            vals = {'server_id': self.id, 'ts': ts, 'queryid': _to_str(item['queryid'], 40),
                    # defense in depth: the collector already redacts literals
                    'query': redact_sql(_to_str(item.get('query') or '', 4000))
                    or ('(query text unavailable)' if item.get('query_unavailable') else ''),
                    'reason': _to_str(item.get('reason') or '', 128)}
            for fname in SQL_COUNTER_FIELDS:
                vals[fname] = _to_counter(item.get(fname))
            for fname in SQL_FLOAT_FIELDS:
                vals[fname] = _to_float(item.get(fname)) or 0.0
            rows.append(vals)
        return rows, rejected

    def _prepare_live(self, items, window):
        categories = dict(self.env['perf.live.query']._fields['category'].selection)
        rows, rejected = [], 0
        for item in items:
            ts = _to_dt(item.get('ts'))
            try:
                pid = _to_int(item.get('pid'), 'pid')
            except ValueError:  # out-of-range int4: only this row is refused
                pid = None
            if not _in_window(ts, window) or pid is None:
                rejected += 1
                continue
            vals = {'server_id': self.id, 'ts': ts, 'pid': pid,
                    'category': item.get('category') if item.get('category') in categories else 'long_running',
                    'xact_start': _to_dt(item.get('xact_start')) or False,
                    'query_start': _to_dt(item.get('query_start')) or False,
                    'duration_s': _to_float(item.get('duration_s')) or 0.0,
                    # defense in depth: the collector already redacts literals
                    'query': redact_sql(_to_str(item.get('query') or '', 2000))}
            for fname in LIVE_CHAR_FIELDS:
                vals[fname] = _to_str(item.get(fname), 255)
            rows.append(vals)
        return rows, rejected

    # ------------------------------------------------------------------ navigation
    def _open_related(self, model, title, view_mode='tree,form'):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': title, 'res_model': model, 'view_mode': view_mode,
            'domain': [('server_id', '=', self.id)], 'context': {'default_server_id': self.id},
        }

    def action_open_metrics(self):
        return self._open_related('perf.metric', _('Metrics'), 'tree,graph,pivot')

    def action_open_sql(self):
        return self._open_related('perf.sql.snapshot', _('Top SQL'))

    def action_open_live(self):
        return self._open_related('perf.live.query', _('Live Queries'))

    def action_open_slow_calls(self):
        action = self._open_related('perf.agent.slow.call.remote', _('Slow calls'))
        action['context'] = dict(action['context'], search_default_calls=1)
        return action
