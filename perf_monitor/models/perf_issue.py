# -*- coding: utf-8 -*-
"""Performance issues ("tasks") built by rules from the slow calls pushed by perf_monitor_agent.

One issue per fingerprint (SQL fingerprint of an N+1 / slow statement, model.method, cron, route or
dashboard tile), shared by every source DB (one tag per DB). Rules only: nothing here calls an LLM
(see perf_ai.py). Issues are never closed automatically; a closed issue that happens again is
reopened (same record) and gets the new DB tag.

Only actionable slowness makes (or updates) an issue: a call where SQL is >= 50 % of the time or one
query takes >= 1 s, an N+1 (same SQL >= 50 times), or a key seen >= 3 times in 7 days (the plain web
client routes never count as recurring). Other slow calls are kept without an issue. Slow calls
matched by an active skip rule (perf.skip.rule) are kept too, counted on the rule, and never create,
reopen or escalate an issue.
"""
import fnmatch
import hashlib
import html
import json
import logging
import re
import threading
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import mute_logger

from .perf_ai_text import best_callsite, real_callsite
from .perf_common import get_float_param, get_int_param
from .perf_redact import redact_sql
from .sql_fingerprint import redact_literals

_logger = logging.getLogger(__name__)

ISSUE_TYPES = [
    ('n_plus_1', 'N+1 queries'),
    ('slow_sql', 'Slow SQL'),
    ('slow_tile', 'Slow dashboard tile'),
    ('slow_method', 'Slow method'),
    ('slow_cron', 'Slow cron'),
    ('slow_route', 'Slow request'),
]
SEVERITIES = [('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')]
SEVERITY_RANK = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}
STATES = [('new', 'New'), ('open', 'Open'), ('skipped', 'Skipped'), ('closed', 'Closed')]
STATE_RANK = {'new': 0, 'open': 1, 'closed': 2, 'skipped': 3}

DEFAULT_MIN_MS = 2000          # perf_monitor.issue_min_ms: calls faster than this never make an issue
N1_MIN_REPEATS = 100           # same statement >= N times in ONE call = N+1
SLOW_SQL_SHARE = 0.5           # one statement >= 50 % of the call time = slow SQL issue
BIG_TABLE_ROWS = 100000        # seq scan on a table with >= this many rows counts in the severity
HUGE_TABLE_ROWS = 1000000
LAST_SEEN_HINT_DAYS = 5
BUILD_BATCH = 2000             # slow call rows handled per build (cron / push)
MAX_EXPLAINS_PER_BUILD = 5
EXPLAIN_TIMEOUT_MS = 1000
MAX_LINKED_TEXT = 256
CALL_GROUPS_LIMIT = 30
CALL_GROUPS_MEMO = 'perf_monitor.call_groups'   # key of cr.cache, see perf.issue._call_groups
BUILD_LOCK_KEY = 0x70657266    # pg advisory lock ("perf"): one build at a time, others skip
IGNORED_MODEL_PREFIXES = ('perf.',)
ASSIGNEE_PARAM = 'perf_monitor.issue_assignee_id'   # Settings > Issue assignee (res.users id)
DEFAULT_ASSIGNEE_HINT = 'yasir'                     # unset: the user whose login / name contains this
# activities only, never an e-mail: no "you have been assigned" mail, no activity notification mail
NO_MAIL_CTX = {'mail_activity_quick_update': True, 'mail_auto_subscribe_no_notify': True,
               'mail_notify_force_send': False}
IGNORED_ROUTE_PREFIXES = ('/perf_monitor/',)
IGNORED_CONTROLLERS = ('PerfMonitorController',)
# actionable filter (Settings > Performance; defaults applied on read: saving 0 deletes the parameter)
DEFAULT_MIN_SQL_SHARE = 0.5    # perf_monitor.issue_min_sql_share: SQL time >= 50 % of the call
DEFAULT_MIN_QUERY_MS = 1000    # perf_monitor.issue_min_query_ms: one query >= 1 s
DEFAULT_MIN_REPEATS = 50       # perf_monitor.issue_min_repeats: same SQL >= 50 times in one call
DEFAULT_MIN_OCC_7D = 3         # perf_monitor.issue_min_occ_7d: same issue key >= 3 times in 7 days
QUIET_ROUTES = ('/', '/web', '/web/login', '/web/assets/*', '/web/webclient/*')  # never "recurring"
_RX_PLACEHOLDER = re.compile(r"\?|%\([^)]*\)s|%s|\$[0-9]+")
_RX_SELECT = re.compile(r"^\s*(select|with)\b", re.I)
_RX_DML = re.compile(r"\b(insert|update|delete|merge|truncate|alter|drop|create|grant|copy|vacuum|lock)\b", re.I)


# ---------------------------------------------------------------------------- pure rules (tested)
def _is_ignored(call):
    """perf_monitor's own requests (its views, its push endpoints) never become issues."""
    model = call.get('model_name') or ''
    route = call.get('route') or ''
    return (model.startswith(IGNORED_MODEL_PREFIXES) or route.startswith(IGNORED_ROUTE_PREFIXES)
            or model in IGNORED_CONTROLLERS)


def call_duration(call):
    """Time of a slow call row; a tile row is judged on its own tile time."""
    if call.get('kind') == 'tile':
        return float(call.get('tile_ms') or call.get('duration_ms') or 0.0)
    return float(call.get('duration_ms') or 0.0)


def classify_call(call, min_ms=DEFAULT_MIN_MS):
    """(issue_type, key, title) for one slow call row (dict of perf.agent.slow.call.remote values),
    or None when the row does not make an issue. Priority: dashboard tile > N+1 > slow SQL >
    cron > model.method > route. The call row of a multi-tile dashboard call is skipped: its tile
    rows make one issue per tile."""
    if _is_ignored(call):
        return None
    if call_duration(call) < min_ms:
        return None
    fingerprint = call.get('top_sql_fingerprint') or ''
    method = '.'.join(p for p in (call.get('model_name'), call.get('method')) if p)
    dashboard, tile = call.get('ui_dashboard') or '', call.get('ui_tile') or ''
    if tile and (call.get('kind') == 'tile' or (call.get('tile_count') or 0) <= 1):
        return ('slow_tile', 'tile:%s|%s' % (dashboard, tile),
                "Slow tile '%s'%s" % (tile, " on dashboard '%s'" % dashboard if dashboard else ''))
    if call.get('kind') != 'tile' and (call.get('tile_count') or 0) > 1:
        return None  # multi-tile call: covered by its tile rows
    where = method or call.get('cron_name') or call.get('route') or 'request'
    callsite = real_callsite(call.get('top_sql_callsite'))   # never wags_logger's cursor patch / the SQL layer
    if fingerprint and (call.get('top_sql_calls') or 0) >= N1_MIN_REPEATS:
        return ('n_plus_1', 'n1:%s' % fingerprint,
                'N+1 in %s: same SQL %d times in one call%s' % (
                    where, call.get('top_sql_calls') or 0, ' at %s' % callsite if callsite else ''))
    duration = float(call.get('duration_ms') or 0.0)
    if fingerprint and duration and float(call.get('top_sql_ms') or 0.0) >= SLOW_SQL_SHARE * duration:
        return ('slow_sql', 'sql:%s' % fingerprint,
                'Slow SQL in %s%s' % (where, ' at %s' % callsite if callsite else ''))
    if call.get('cron_name'):
        return ('slow_cron', 'cron:%s' % call['cron_name'], "Slow cron '%s'" % call['cron_name'])
    if method:
        return ('slow_method', 'method:%s' % method, 'Slow method %s' % method)
    if call.get('route'):
        return ('slow_route', 'route:%s' % call['route'], 'Slow request %s' % call['route'])
    return None


def is_quiet_route(route):
    """Plain web client routes (home, login, assets): slow there only with a SQL / N+1 reason."""
    return bool(route) and any(fnmatch.fnmatchcase(route, pattern) for pattern in QUIET_ROUTES)


def actionable_reason(call, min_sql_share=DEFAULT_MIN_SQL_SHARE, min_query_ms=DEFAULT_MIN_QUERY_MS,
                      min_repeats=DEFAULT_MIN_REPEATS):
    """Why one slow call row alone is worth an issue ('sql_share', 'slow_query', 'n_plus_1'), else None.
    The third reason (recurring: same key >= N times in 7 days) needs the DB: see
    PerfIssue._is_actionable."""
    duration = float(call.get('duration_ms') or 0.0)
    if duration and float(call.get('sql_ms') or 0.0) >= min_sql_share * duration:
        return 'sql_share'
    if float(call.get('top_sql_ms') or 0.0) >= min_query_ms:
        return 'slow_query'
    if (call.get('top_sql_calls') or 0) >= min_repeats:
        return 'n_plus_1'
    return None


def issue_fingerprint(key):
    return hashlib.sha1(key.encode('utf-8')).hexdigest()


def severity_for(max_ms=0.0, count_7d=0, max_sql_count=0, max_repeats=0, seq_scan_rows=0):
    """(severity, score, [reasons]) from the rule table below. The reasons double as the rule hint
    given to the AI layer."""
    score, reasons = 0, []

    def hit(points, text):
        nonlocal score
        score += points
        reasons.append('+%d %s' % (points, text))

    seconds = (max_ms or 0.0) / 1000.0
    if seconds >= 60:
        hit(3, 'slowest call %.1f s >= 60 s' % seconds)
    elif seconds >= 10:
        hit(2, 'slowest call %.1f s >= 10 s' % seconds)
    elif seconds >= 3:
        hit(1, 'slowest call %.1f s >= 3 s' % seconds)
    if count_7d >= 50:
        hit(2, '%d occurrences in 7 days >= 50' % count_7d)
    elif count_7d >= 10:
        hit(1, '%d occurrences in 7 days >= 10' % count_7d)
    if max_sql_count >= 10000:
        hit(2, '%d SQL statements in one call >= 10000' % max_sql_count)
    elif max_sql_count >= 1000:
        hit(1, '%d SQL statements in one call >= 1000' % max_sql_count)
    if max_repeats >= 1000:
        hit(3, 'N+1: same statement %d times in one call >= 1000' % max_repeats)
    elif max_repeats >= N1_MIN_REPEATS:
        hit(2, 'N+1: same statement %d times in one call >= %d' % (max_repeats, N1_MIN_REPEATS))
    if seq_scan_rows >= HUGE_TABLE_ROWS:
        hit(2, 'seq scan on a table of %d rows >= %d' % (seq_scan_rows, HUGE_TABLE_ROWS))
    elif seq_scan_rows >= BIG_TABLE_ROWS:
        hit(1, 'seq scan on a table of %d rows >= %d' % (seq_scan_rows, BIG_TABLE_ROWS))
    severity = 'critical' if score >= 7 else 'high' if score >= 5 else 'medium' if score >= 3 else 'low'
    return severity, score, reasons


def explainable_sql(sql):
    """Redacted statement -> text for EXPLAIN (GENERIC_PLAN): placeholders / ``?`` -> $1..$n.
    None for anything but a plain SELECT / WITH ... SELECT (EXPLAIN without ANALYZE never runs
    the statement, but DML is refused anyway)."""
    if not sql or not _RX_SELECT.match(sql) or _RX_DML.search(sql):
        return None
    counter = iter(range(1, 10000))
    return _RX_PLACEHOLDER.sub(lambda m: '$%d' % next(counter), sql.strip().rstrip(';'))


def plan_nodes(plan):
    """Flatten an EXPLAIN (FORMAT JSON) plan into its nodes."""
    stack = [plan[0]['Plan']] if isinstance(plan, list) and plan and 'Plan' in plan[0] else []
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get('Plans') or [])


def plan_summary(plan, limit=40):
    """Compact, literal-free text of a JSON plan (node, relation, index, rows, cost, filter)."""
    lines = []
    for node in plan_nodes(plan):
        parts = [node.get('Node Type', '?')]
        if node.get('Relation Name'):
            parts.append('on %s' % node['Relation Name'])
        if node.get('Index Name'):
            parts.append('using %s' % node['Index Name'])
        parts.append('rows=%s cost=%s' % (node.get('Plan Rows'), node.get('Total Cost')))
        for key in ('Index Cond', 'Filter', 'Hash Cond', 'Join Filter', 'Sort Key', 'Group Key'):
            if node.get(key):
                value = node[key] if isinstance(node[key], str) else ', '.join(map(str, node[key]))
                parts.append('%s: %s' % (key, redact_literals(value)[:200]))
        lines.append(' '.join(parts))
        if len(lines) >= limit:
            lines.append('...')
            break
    return '\n'.join(lines)


def _merge_text(current, values, size=MAX_LINKED_TEXT):
    """Comma separated distinct values (kept order), capped."""
    items = [v for v in (current or '').split(', ') if v]
    for value in values:
        if value and value not in items:
            items.append(value)
    text = ', '.join(items)
    return text if len(text) <= size else text[:size - 3] + '...'


def _seconds(ms):
    return '%.1f s' % ((ms or 0.0) / 1000.0)


def plain_explanation(v):
    """English + Roman Urdu summary of an issue (dict of perf.issue values) for an ERP admin: where it
    comes from, model, users, DB vs Odoo time, top query, likely fix, how to reproduce. Rules only."""
    dashboard, tile, menu, screen = v.get('dashboard'), v.get('tile'), v.get('menu'), v.get('screen')
    cron, route = v.get('cron_name'), v.get('route')
    where = []
    if dashboard:
        where.append("dashboard '%s'" % dashboard + (" > tile '%s'" % tile if tile else ''))
    elif tile:
        where.append("tile '%s'" % tile)
    if menu:
        where.append("menu '%s'" % menu)
    if screen:
        where.append("screen '%s'" % screen)
    if cron:
        where.append("scheduled action '%s'" % cron)
    if route:
        where.append('URL %s' % route)
    where = ', '.join(where) or '?'
    code = '.'.join(p for p in (v.get('model_name'), v.get('method')) if p) or '?'
    users = v.get('users') or '?'
    dbs = v.get('databases') or '?'
    total = v.get('max_duration_ms') or 0.0
    sql = min(v.get('sql_ms') or 0.0, total)
    if total and sql:
        db_pct = int(round(100.0 * sql / total))
        split_en = 'slowest call %s: database (SQL) %s (%d %%), Odoo / Python %s (%d %%)' % (
            _seconds(total), _seconds(sql), db_pct, _seconds(total - sql), 100 - db_pct)
        split_ur = 'sab se slow call %s: database (SQL) %s (%d %%), Odoo / Python %s (%d %%)' % (
            _seconds(total), _seconds(sql), db_pct, _seconds(total - sql), 100 - db_pct)
    else:
        db_pct = None
        split_en = 'slowest call %s (database time not reported)' % _seconds(total)
        split_ur = 'sab se slow call %s (database ka waqt nahi aaya)' % _seconds(total)
    repeats = v.get('max_repeats') or 0
    top_sql = ' '.join((v.get('top_sql') or '').split())
    if top_sql:
        top_sql = top_sql if len(top_sql) <= 300 else top_sql[:297] + '...'
        top_en = '%.2f s%s: %s' % ((v.get('top_sql_ms') or 0.0) / 1000.0,
                                   ', up to %d times in one call' % repeats if repeats > 1 else '', top_sql)
        top_ur = '%.2f s%s: %s' % ((v.get('top_sql_ms') or 0.0) / 1000.0,
                                   ', aik call mein %d dafa tak' % repeats if repeats > 1 else '', top_sql)
    else:
        top_en = top_ur = '?'
    at = ' at %s' % v['callsite'] if v.get('callsite') else ''
    at_ur = ' (%s)' % v['callsite'] if v.get('callsite') else ''
    fix_en, fix_ur = [], []
    if v.get('issue_type') == 'n_plus_1' or repeats >= DEFAULT_MIN_REPEATS:
        fix_en.append('N+1 in the code%s: the same query runs %d times in one call. Read the data once outside the '
                      'loop (one search / read_group / mapped on the whole recordset) instead of one query per '
                      'record.' % (at, repeats))
        fix_ur.append('Code mein N+1%s: aik hi query aik call mein %d dafa chal rahi hai. Loop ke andar har record '
                      'ke liye query na chalayen; data aik dafa loop se bahar laayen (poore recordset par search / '
                      'read_group / mapped).' % (at_ur, repeats))
    if v.get('seq_scan_tables'):
        fix_en.append('Index: PostgreSQL reads the whole table %s; add an index on the filtered columns (see '
                      'EXPLAIN).' % v['seq_scan_tables'])
        fix_ur.append('Index: PostgreSQL poora table %s parh raha hai; filter wale columns par index lagayen '
                      '(EXPLAIN dekhen).' % v['seq_scan_tables'])
    if v.get('issue_type') == 'slow_sql' or (v.get('top_sql_ms') or 0.0) >= DEFAULT_MIN_QUERY_MS:
        fix_en.append('Slow query / domain: check the filter (non-stored or computed fields, ilike on large text, '
                      'many OR conditions, no limit) and add an index if EXPLAIN shows a Seq Scan.')
        fix_ur.append('Slow query / domain: filter check karen (non-stored ya computed fields, bare text par ilike, '
                      'bohat se OR, limit nahi) aur agar EXPLAIN mein Seq Scan ho to index lagayen.')
    if db_pct is not None and db_pct < 30 and not fix_en:
        fix_en.append('Python code: most of the time is in Odoo, not in the database; profile %s (loops, computed '
                      'fields, reports).' % code)
        fix_ur.append('Python code: zyada waqt database mein nahi balkay Odoo code mein lag raha hai; %s ko profile '
                      'karen (loops, computed fields, reports).' % code)
    if not fix_en:
        fix_en.append('Check the slow calls of this issue and the AI analysis tab.')
        fix_ur.append('Is issue ki slow calls aur AI analysis tab dekhen.')
    user = users.split(', ')[0]
    avg = _seconds(v.get('avg_duration_ms') or total)
    if cron:
        repro_en = ("On DB %s run the scheduled action '%s' (Settings > Technical > Scheduled Actions > Run "
                    "Manually); it takes about %s. Then check Performance > Slow Calls." % (dbs, cron, avg))
        repro_ur = ("DB %s par scheduled action '%s' ko Run Manually karen (Settings > Technical > Scheduled "
                    "Actions); taqreeban %s lagta hai. Phir Performance > Slow Calls dekhen." % (dbs, cron, avg))
    else:
        repro_en = ('On DB %s log in as %s and open %s; it takes about %s. Then check Performance > Slow Calls.'
                    % (dbs, user, where, avg))
        repro_ur = ('DB %s par %s ke tor par login karen aur %s kholen; taqreeban %s lagta hai. Phir Performance > '
                    'Slow Calls dekhen.' % (dbs, user, where, avg))
    return '\n'.join([
        'English',
        '- Where: %s' % where,
        '- Model / method: %s' % code,
        '- Users: %s' % users,
        '- Time: %s' % split_en,
        '- Top query: %s' % top_en,
        '- Likely fix: %s' % ' '.join(fix_en),
        '- How to reproduce: %s' % repro_en,
        '',
        'Roman Urdu',
        '- Kahan se: %s' % where,
        '- Model / method: %s' % code,
        '- Users: %s' % users,
        '- Waqt: %s' % split_ur,
        '- Sab se bari query: %s' % top_ur,
        '- Mumkina hal: %s' % ' '.join(fix_ur),
        '- Dobara kaise dekhen: %s' % repro_ur,
    ])


# ---------------------------------------------------------------------------- models
class PerfIssueTag(models.Model):
    _name = 'perf.issue.tag'
    _description = 'Performance Issue DB Tag'
    _order = 'name'

    name = fields.Char(required=True, index=True)
    color = fields.Integer()

    _sql_constraints = [('name_uniq', 'unique(name)', 'This DB tag already exists.')]

    @api.model
    def _get_or_create(self, names):
        names = sorted({n for n in names if n})
        tags = self.search([('name', 'in', names)])
        missing = [n for n in names if n not in tags.mapped('name')]
        return tags | self.create([{'name': n, 'color': (sum(map(ord, n)) % 11) + 1} for n in missing])


class PerfIssue(models.Model):
    _name = 'perf.issue'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Performance Issue'
    _order = 'state_rank, severity_rank desc, last_seen desc, id desc'

    name = fields.Char('Issue', required=True, tracking=True)
    fingerprint = fields.Char(required=True, index=True, readonly=True, copy=False,
                              help='sha1 of the issue key: one issue (task) per key')
    key = fields.Char('Issue key', readonly=True, help='n1:/sql:<SQL fingerprint>, tile:<dashboard>|<tile>, '
                                                       'method:<model.method>, cron:<name>, route:<path>')
    issue_type = fields.Selection(ISSUE_TYPES, 'Type', required=True, readonly=True)
    state = fields.Selection(STATES, default='new', required=True, tracking=True)
    state_rank = fields.Integer(compute='_compute_ranks', store=True)
    severity = fields.Selection(SEVERITIES, default='low', required=True, readonly=True, tracking=True)
    severity_rank = fields.Integer(compute='_compute_ranks', store=True)
    severity_score = fields.Integer(readonly=True)
    severity_reasons = fields.Text('Rule hits', readonly=True)
    user_id = fields.Many2one('res.users', 'Assigned to', tracking=True)
    tag_ids = fields.Many2many('perf.issue.tag', string='Databases')
    server_ids = fields.Many2many('perf.server', string='Sources')
    # evidence
    slow_call_ids = fields.One2many('perf.agent.slow.call.remote', 'issue_id', 'Slow calls')
    slow_call_count = fields.Integer(compute='_compute_slow_call_count')
    occurrence_count = fields.Integer('Occurrences', readonly=True,
                                      help='Slow calls counted for this issue (kept after the calls are purged)')
    count_7d = fields.Integer('Occurrences 7d', readonly=True)
    first_seen = fields.Datetime(readonly=True)
    last_seen = fields.Datetime(readonly=True, index=True)
    last_seen_days = fields.Integer(compute='_compute_last_seen_hint')
    age_days = fields.Integer('Age (days)', compute='_compute_age_days',
                              help='Days since the issue was created (sort the list on "Created")')
    last_seen_hint = fields.Char(compute='_compute_last_seen_hint')
    max_duration_ms = fields.Float('Slowest (ms)', digits=(16, 1), readonly=True, group_operator='max')
    total_duration_ms = fields.Float('Total (ms)', digits=(16, 1), readonly=True)
    avg_duration_ms = fields.Float('Average (ms)', digits=(16, 1), compute='_compute_avg')
    # the UI shows seconds; the *_ms columns stay the stored values (rules, sorting, settings)
    max_duration_s = fields.Float('Slowest (s)', digits=(16, 2), compute='_compute_seconds')
    avg_duration_s = fields.Float('Average (s)', digits=(16, 2), compute='_compute_seconds')
    top_sql_s = fields.Float('Top SQL (s)', digits=(16, 2), compute='_compute_seconds',
                             help='Time of the top SQL in the slowest call')
    sql_s = fields.Float('SQL time of the slowest call (s)', digits=(16, 2), compute='_compute_seconds')
    max_sql_count = fields.Integer('Max SQL / call', readonly=True)
    max_repeats = fields.Integer('Max same SQL / call', readonly=True, help='N+1 indicator')
    top_sql = fields.Text('Top SQL (redacted)', readonly=True)
    top_sql_ms = fields.Float('Top SQL (ms)', digits=(16, 1), readonly=True)
    sql_ms = fields.Float('SQL time of the slowest call (ms)', digits=(16, 1), readonly=True)
    top_sql_fingerprint = fields.Char('SQL fingerprint', readonly=True, index=True)
    callsite = fields.Char('Python call site', readonly=True)
    model_name = fields.Char('Model', readonly=True)
    method = fields.Char(readonly=True)
    cron_name = fields.Char('Cron', readonly=True)
    route = fields.Char(readonly=True)
    dashboard = fields.Char(readonly=True)
    tile = fields.Char(readonly=True)
    menu = fields.Char(readonly=True)
    screen = fields.Char('Screen', readonly=True, help='Action / screen model the users were on')
    users = fields.Char(readonly=True)
    explain_plan = fields.Text('EXPLAIN (generic plan, no ANALYZE)', readonly=True)
    seq_scan_tables = fields.Char('Seq scans on big tables', readonly=True)
    seq_scan_rows = fields.Float('Largest seq-scanned table (rows)', digits=(20, 0), readonly=True)
    reopen_count = fields.Integer(readonly=True)
    closed_date = fields.Datetime(readonly=True)
    closed_by = fields.Many2one('res.users', readonly=True)
    skip_rule_id = fields.Many2one('perf.skip.rule', 'Skip rule', readonly=True, ondelete='set null',
                                   help='Rule created by the Skip button (kept after the issue reopens)')
    call_group_count = fields.Integer('Call groups', compute='_compute_call_breakdown',
                                      help='Distinct source DB / model.method / cron / route / user / call site '
                                           'among the slow calls of this issue')
    call_breakdown = fields.Html('Per-call breakdown', compute='_compute_call_breakdown', sanitize=False)
    explanation = fields.Text('In plain words', compute='_compute_explanation', store=True,
                              help='Where from, model, users, DB vs Odoo time, top query, likely fix and how to '
                                   'reproduce, in English and Roman Urdu (rules, no AI)')

    _sql_constraints = [('fingerprint_uniq', 'unique(fingerprint)', 'One issue per fingerprint.')]

    @api.depends('state', 'severity')
    def _compute_ranks(self):
        for issue in self:
            issue.state_rank = STATE_RANK.get(issue.state, 1)
            issue.severity_rank = SEVERITY_RANK.get(issue.severity, 0)

    def _compute_slow_call_count(self):
        counts = {}
        if self.ids:
            groups = self.env['perf.agent.slow.call.remote'].read_group(
                [('issue_id', 'in', self.ids)], ['issue_id'], ['issue_id'])
            counts = {g['issue_id'][0]: g['issue_id_count'] for g in groups}
        for issue in self:
            issue.slow_call_count = counts.get(issue.id, 0)

    @api.depends('last_seen')
    def _compute_last_seen_hint(self):
        now = fields.Datetime.now()
        for issue in self:
            days = (now - issue.last_seen).days if issue.last_seen else 0
            issue.last_seen_days = days
            issue.last_seen_hint = (_('Last seen %d days ago') % days
                                    if issue.last_seen and days >= LAST_SEEN_HINT_DAYS else False)

    @api.depends('create_date')
    def _compute_age_days(self):
        now = fields.Datetime.now()
        for issue in self:
            # never negative: create_date is the transaction start and may be a few microseconds ahead of the Python clock
            issue.age_days = max(0, (now - issue.create_date).days) if issue.create_date else 0

    @api.depends('total_duration_ms', 'occurrence_count')
    def _compute_avg(self):
        for issue in self:
            issue.avg_duration_ms = issue.total_duration_ms / issue.occurrence_count if issue.occurrence_count else 0.0

    @api.depends('max_duration_ms', 'avg_duration_ms', 'top_sql_ms', 'sql_ms')
    def _compute_seconds(self):
        for issue in self:
            issue.max_duration_s = (issue.max_duration_ms or 0.0) / 1000.0
            issue.avg_duration_s = (issue.avg_duration_ms or 0.0) / 1000.0
            issue.top_sql_s = (issue.top_sql_ms or 0.0) / 1000.0
            issue.sql_s = (issue.sql_ms or 0.0) / 1000.0

    def _call_groups(self, limit=CALL_GROUPS_LIMIT):
        """Slow calls of this issue grouped by source (DB, model.method / cron / route, user, call site),
        worst first: an issue can merge calls with different origins that need separate fixes.

        One aggregated SQL query (the issue may hold thousands of calls), memoised per cursor: the form
        computes it for the breakdown table, the AI context and the Claude Code prompt. The memo is dropped
        by any create / write / unlink / purge of slow calls (CALL_GROUPS_MEMO).

        cr.cache is a plain dict of the Cursor (odoo/sql_db.py): commit() / rollback() do NOT clear it. A stale
        memo is only possible when slow calls are changed inside a savepoint that is rolled back AFTER the memo
        was filled in it (tests); a request or cron job gets a new cursor, hence an empty memo."""
        self.ensure_one()
        memo = self.env.cr.cache.setdefault(CALL_GROUPS_MEMO, {})
        memo_key = (self.id, limit)
        if memo_key in memo:
            return [dict(group) for group in memo[memo_key]]
        Remote = self.env['perf.agent.slow.call.remote']
        Remote.flush_model(['issue_id', 'db_name', 'cron_name', 'model_name', 'method', 'route', 'login',
                            'top_sql_callsite', 'kind', 'duration_ms', 'tile_ms', 'sql_count', 'top_sql_calls', 'ts'])
        self.env.cr.execute("""
            SELECT db_name, cron_name, model_name, method, route, login, top_sql_callsite,
                   count(*), max(d), sum(d), max(COALESCE(sql_count, 0)), max(COALESCE(top_sql_calls, 0)),
                   min(ts), max(ts)
              FROM (SELECT *, CASE WHEN kind = 'tile' THEN COALESCE(NULLIF(tile_ms, 0), duration_ms, 0)
                                   ELSE COALESCE(duration_ms, 0) END AS d
                      FROM perf_agent_slow_call_remote WHERE issue_id = %s) calls
             GROUP BY 1, 2, 3, 4, 5, 6, 7
        """, (self.id,))
        groups = {}
        for (db, cron, model, method, route, login, callsite, calls, max_ms, total_ms, max_sql, max_rep,
             first, last) in self.env.cr.fetchall():
            where = 'cron %s' % cron if cron else '.'.join(p for p in (model, method) if p) or route or '?'
            key = (db or '?', where, login or '?', callsite or '')
            group = groups.setdefault(key, {'db': key[0], 'where': where, 'user': key[2], 'callsite': key[3],
                                            'calls': 0, 'max_s': 0.0, 'total_s': 0.0, 'max_sql_count': 0,
                                            'max_repeats': 0, 'first': first, 'last': last})
            group['calls'] += calls
            group['max_s'] = max(group['max_s'], (max_ms or 0.0) / 1000.0)
            group['total_s'] += (total_ms or 0.0) / 1000.0
            group['max_sql_count'] = max(group['max_sql_count'], max_sql or 0)
            group['max_repeats'] = max(group['max_repeats'], max_rep or 0)
            group['first'] = min(group['first'], first)
            group['last'] = max(group['last'], last)
        rows = sorted(groups.values(), key=lambda g: (-g['max_s'], -g['calls']))[:limit]
        for group in rows:
            group['avg_s'] = round(group.pop('total_s') / group['calls'], 2)
            group['max_s'] = round(group['max_s'], 2)
            group['first'], group['last'] = str(group['first']), str(group['last'])
            for text_key in ('db', 'where', 'user', 'callsite'):
                if len(group[text_key]) > 200:
                    group[text_key] = group[text_key][:197] + '...'
        memo[memo_key] = [dict(group) for group in rows]
        return rows

    @api.depends('slow_call_ids')
    def _compute_call_breakdown(self):
        headers = ('DB', 'Model.method / cron / route', 'User', 'Call site', 'Calls', 'Slowest (s)', 'Average (s)',
                   'Max SQL / call', 'Max same SQL', 'First (UTC)', 'Last (UTC)')
        keys = ('db', 'where', 'user', 'callsite', 'calls', 'max_s', 'avg_s', 'max_sql_count', 'max_repeats',
                'first', 'last')
        for issue in self:
            rows = issue._call_groups() if issue.id else []
            issue.call_group_count = len(rows)
            if len(rows) < 2:
                issue.call_breakdown = False
                continue
            body = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % html.escape(str(row[k])) for k in keys)
                           for row in rows)
            issue.call_breakdown = (
                '<table class="table table-sm table-striped o_perf_breakdown"><thead><tr>%s</tr></thead>'
                '<tbody>%s</tbody></table>' % (''.join('<th>%s</th>' % h for h in headers), body))

    @api.depends('issue_type', 'dashboard', 'tile', 'menu', 'screen', 'cron_name', 'route', 'model_name', 'method',
                 'users', 'tag_ids', 'max_duration_ms', 'total_duration_ms', 'occurrence_count', 'sql_ms', 'top_sql',
                 'top_sql_ms', 'max_repeats', 'callsite', 'seq_scan_tables')
    def _compute_explanation(self):
        for issue in self:
            issue.explanation = plain_explanation(issue._explanation_vals())

    def _explanation_vals(self):
        vals = {
            fname: self[fname] for fname in (
                'issue_type', 'dashboard', 'tile', 'menu', 'screen', 'cron_name', 'route', 'model_name',
                'method', 'users', 'max_duration_ms', 'avg_duration_ms', 'sql_ms', 'top_sql', 'top_sql_ms',
                'max_repeats', 'callsite', 'seq_scan_tables')
        }
        vals['databases'] = ', '.join(self.tag_ids.mapped('name'))  # no dict | dict: staging runs Python 3.8
        return vals

    def _redacted_explanation(self):
        """Plain explanation with the top SQL redacted again (literal values removed), for text leaving
        the server (Claude Code prompt)."""
        self.ensure_one()
        vals = self._explanation_vals()
        vals['top_sql'] = strip_sql(vals['top_sql'])
        return plain_explanation(vals)

    # ------------------------------------------------------------------ build (cron / push)
    @api.model
    def _cron_build_issues(self):
        """Cron "Perf Monitor: build issues": rules, then the AI layer for new issues."""
        self._assign_unassigned()
        self.env['perf.skip.rule']._archive_expired()
        self._build_issues()
        self._ai_analyse_pending()
        return True

    @api.model
    def _build_issues_safe(self):
        """Called after every accepted slow calls push (staging runs without cron threads). Never
        raises: a failure (or a concurrent build) only leaves the rows for the next build."""
        try:
            with self.env.cr.savepoint():
                return self._build_issues()
        except Exception as exc:  # pylint: disable=broad-except
            _logger.warning('perf_monitor: issue build after push skipped: %s', exc)
            return 0

    @api.model
    def _build_issues(self, batch=BUILD_BATCH):
        """Turn the slow call rows not handled yet into issues. Returns the number of rows handled."""
        cr = self.env.cr
        cr.execute('SELECT pg_try_advisory_xact_lock(%s)', (BUILD_LOCK_KEY,))
        if not cr.fetchone()[0]:
            return 0
        Call = self.env['perf.agent.slow.call.remote']
        Call.flush_model()
        cr.execute('SELECT id FROM perf_agent_slow_call_remote WHERE issue_checked IS NOT TRUE '
                   'ORDER BY id LIMIT %s', (batch,))
        calls = Call.browse([r[0] for r in cr.fetchall()])
        if not calls:
            return 0
        min_ms = get_int_param(self.env, 'perf_monitor.issue_min_ms', DEFAULT_MIN_MS)
        thresholds = self._issue_thresholds()
        rules = self.env['perf.skip.rule']._active_rules()
        groups, titles, keyed, skipped = {}, {}, {}, {}
        for call in calls:
            values = call._issue_values()
            found = classify_call(values, min_ms)
            if not found:
                continue
            itype, key, title = found
            keyed[key] = keyed.get(key, Call) | call
            rule = rules._match(dict(values, issue_key=key))
            if rule:  # kept, counted on the rule, never an issue
                skipped[rule] = skipped.get(rule, Call) | call
                continue
            if (itype, key) not in groups or call_duration(values) > titles[(itype, key)][0]:
                titles[(itype, key)] = (call_duration(values), title)
            groups[(itype, key)] = groups.get((itype, key), Call) | call
        for key, rows in keyed.items():
            rows.write({'issue_key': key[:512]})
        for rule, rows in skipped.items():
            rows.write({'skip_rule_id': rule.id})
            rule._register_hits(rows)
        new_issues = self.browse()
        reopened_issues = self.browse()
        touched = self.browse()
        for (itype, key), rows in groups.items():
            if not self._is_actionable(key, rows, thresholds):
                continue  # kept without an issue: may still count as recurring later
            rows |= self._earlier_rows(key, rows)
            issue, created, reopened = self._upsert_from_calls(itype, key, titles[(itype, key)][1], rows)
            touched |= issue
            if created:
                new_issues |= issue
            elif reopened:
                reopened_issues |= issue
        calls.write({'issue_checked': True})
        touched._refresh_count_7d()
        for issue in new_issues[:MAX_EXPLAINS_PER_BUILD]:
            issue._run_explain()
        touched._update_severity()
        new_issues._schedule_assignee_activity(_('New performance issue'))
        reopened_issues._schedule_assignee_activity(_('Reopened performance issue'))
        _logger.info('perf_monitor: issue build: %d slow calls, %d issues touched, %d new, %d skipped by rules',
                     len(calls), len(touched), len(new_issues), sum(len(r) for r in skipped.values()))
        return len(calls)

    @api.model
    def _issue_thresholds(self):
        """Actionable filter settings; missing / emptied parameters fall back to the defaults."""
        return {
            'min_sql_share': get_float_param(self.env, 'perf_monitor.issue_min_sql_share', DEFAULT_MIN_SQL_SHARE),
            'min_query_ms': get_float_param(self.env, 'perf_monitor.issue_min_query_ms', DEFAULT_MIN_QUERY_MS),
            'min_repeats': get_int_param(self.env, 'perf_monitor.issue_min_repeats', DEFAULT_MIN_REPEATS)
            or DEFAULT_MIN_REPEATS,
            'min_occ_7d': get_int_param(self.env, 'perf_monitor.issue_min_occ_7d', DEFAULT_MIN_OCC_7D)
            or DEFAULT_MIN_OCC_7D,
        }

    @api.model
    def _is_actionable(self, key, rows, thresholds):
        """True when one row has a SQL / N+1 reason, or when the key is recurring (>= N slow calls in
        7 days, skipped ones included) outside the plain web client routes."""
        occ = thresholds['min_occ_7d']
        for row in rows:
            if actionable_reason(row._issue_values(), thresholds['min_sql_share'], thresholds['min_query_ms'],
                                 thresholds['min_repeats']):
                return True
        if all(is_quiet_route(route) for route in rows.mapped('route')):
            return False
        since = fields.Datetime.now() - timedelta(days=7)
        return self.env['perf.agent.slow.call.remote'].search_count(
            [('issue_key', '=', key[:512]), ('ts', '>=', since)], limit=occ) >= occ

    @api.model
    def _earlier_rows(self, key, rows):
        """Slow calls of the last 7 days with this key kept without an issue (not actionable yet, not
        skipped): they become occurrences of the issue."""
        since = fields.Datetime.now() - timedelta(days=7)
        return self.env['perf.agent.slow.call.remote'].search([
            ('issue_key', '=', key[:512]), ('issue_id', '=', False), ('skip_rule_id', '=', False),
            ('issue_checked', '=', True), ('ts', '>=', since), ('id', 'not in', rows.ids)])

    def _upsert_from_calls(self, itype, key, title, rows):
        fingerprint = issue_fingerprint(key)
        issue = self.with_context(active_test=False).search([('fingerprint', '=', fingerprint)], limit=1)
        created = not issue
        reopened = False
        slowest = max(rows, key=lambda r: call_duration(r._issue_values()))
        if created:
            issue = self.with_context(NO_MAIL_CTX, mail_create_nolog=True, mail_create_nosubscribe=True).create({
                'name': title[:256], 'fingerprint': fingerprint, 'key': key[:512], 'issue_type': itype,
                'first_seen': min(rows.mapped('ts')), 'ai_state': 'pending',
                'user_id': self._issue_assignee().id,
            })
            # history in the chatter (a note: nobody is notified, never an e-mail); the occurrences are not logged
            issue._message_log(body=_('Issue created by the rules: %(title)s (%(n)d slow call(s), DB %(dbs)s).',
                                      title=title[:256], n=len(rows),
                                      dbs=', '.join(sorted({d for d in rows.mapped('db_name') if d})) or '?'))
        vals = issue._stats_vals(rows, slowest)
        latest = max(rows.mapped('ts'))
        closed_again = issue.state == 'closed' and issue.closed_date and latest > issue.closed_date
        # skipped: the rows reach this point only when no skip rule matches any more (expired / archived)
        if closed_again or issue.state == 'skipped':
            reopened = True
            vals.update(state='open', reopen_count=issue.reopen_count + 1)
            if not issue.user_id:
                vals['user_id'] = self._issue_assignee().id
            dbs = ', '.join(sorted({d for d in rows.mapped('db_name') if d}))
            if closed_again:
                body = _('Reopened: happened again on %(ts)s (DB %(dbs)s) after it was closed on %(closed)s.',
                         ts=latest, dbs=dbs or '?', closed=issue.closed_date)
            else:
                body = _('Reopened: happened again on %(ts)s (DB %(dbs)s) and no skip rule matches any more.',
                         ts=latest, dbs=dbs or '?')
            issue._message_log(body=body)
        issue.with_context(NO_MAIL_CTX).write(vals)
        rows.write({'issue_id': issue.id})
        return issue, created, reopened

    def _stats_vals(self, rows, slowest):
        self.ensure_one()
        durations = [call_duration(r._issue_values()) for r in rows]
        tags = self.env['perf.issue.tag']._get_or_create(rows.mapped('db_name'))
        vals = {
            'occurrence_count': self.occurrence_count + len(rows),
            'total_duration_ms': self.total_duration_ms + sum(durations),
            'first_seen': min([d for d in (self.first_seen, min(rows.mapped('ts'))) if d]),
            'last_seen': max([d for d in (self.last_seen, max(rows.mapped('ts'))) if d]),
            'max_sql_count': max([self.max_sql_count] + rows.mapped('sql_count')),
            'max_repeats': max([self.max_repeats] + rows.mapped('top_sql_calls')),
            'tag_ids': [(4, t.id) for t in tags],
            'server_ids': [(4, s.id) for s in rows.mapped('server_id')],
            'users': _merge_text(self.users, rows.mapped('login')),
            'menu': _merge_text(self.menu, rows.mapped('ui_menu')),
            'screen': _merge_text(self.screen, [r.ui_action or r.ui_model for r in rows]),
        }
        # the real call site (wags_logger's cursor patch / the SQL layer skipped): slowest call first, then the
        # other calls, slowest first; none: the issue keeps its own real call site, else none (never a wrong one)
        ranked = [slowest] + sorted(rows - slowest, key=lambda r: -call_duration(r._issue_values()))
        found = best_callsite(r.top_sql_callsite for r in ranked)
        if max(durations) >= (self.max_duration_ms or 0.0):
            vals.update({
                'max_duration_ms': max(durations),
                'top_sql': slowest.top_sql or self.top_sql,
                'top_sql_ms': slowest.top_sql_ms,
                'sql_ms': slowest.sql_ms,
                'top_sql_fingerprint': slowest.top_sql_fingerprint or self.top_sql_fingerprint,
                'callsite': (real_callsite(slowest.top_sql_callsite) or found or real_callsite(self.callsite)
                             or False),
                'model_name': slowest.model_name or self.model_name,
                'method': slowest.method or self.method,
                'cron_name': slowest.cron_name or self.cron_name,
                'route': slowest.route or self.route,
                'dashboard': slowest.ui_dashboard or self.dashboard,
                'tile': slowest.ui_tile or self.tile,
            })
        elif found and not real_callsite(self.callsite):
            vals['callsite'] = found
        return vals

    def _refresh_count_7d(self):
        if not self:
            return
        since = fields.Datetime.now() - timedelta(days=7)
        groups = self.env['perf.agent.slow.call.remote'].read_group(
            [('issue_id', 'in', self.ids), ('ts', '>=', since)], ['issue_id'], ['issue_id'])
        counts = {g['issue_id'][0]: g['issue_id_count'] for g in groups}
        for issue in self:
            if issue.count_7d != counts.get(issue.id, 0):
                issue.count_7d = counts.get(issue.id, 0)

    def _update_severity(self):
        for issue in self:
            severity, score, reasons = severity_for(
                issue.max_duration_ms, issue.count_7d, issue.max_sql_count, issue.max_repeats,
                issue.seq_scan_rows)
            vals = {'severity_score': score, 'severity_reasons': '\n'.join(reasons) or False}
            if issue.severity != severity:
                vals['severity'] = severity
            issue.write(vals)

    # ------------------------------------------------------------------ assignee (activity only, no e-mail)
    @api.model
    def _find_default_assignee(self):
        """The user whose login contains 'yasir' (then name), else Administrator."""
        Users = self.env['res.users'].sudo()
        user = (Users.search([('login', 'ilike', DEFAULT_ASSIGNEE_HINT), ('share', '=', False)], order='id', limit=1)
                or Users.search([('name', 'ilike', DEFAULT_ASSIGNEE_HINT), ('share', '=', False)], order='id', limit=1))
        return user or self.env.ref('base.user_admin', raise_if_not_found=False) or Users.browse(2).exists()

    @api.model
    def _issue_assignee(self):
        """Settings > Issue assignee; when unset (or archived) the default is found and stored."""
        user_id = get_int_param(self.env, ASSIGNEE_PARAM, 0)
        user = self.env['res.users'].sudo().browse(user_id).exists() if user_id else None
        if not user or not user.active:
            user = self._find_default_assignee()
            if user:
                self.env['ir.config_parameter'].sudo().set_param(ASSIGNEE_PARAM, str(user.id))
        return user or self.env['res.users']

    @api.model
    def _assign_unassigned(self):
        """Issues without an assignee (built before 16.0.1.7.0) get the Settings assignee; the open
        ones also get a To-Do activity. Returns the issues assigned."""
        issues = self.sudo().with_context(active_test=False).search([('user_id', '=', False)])
        assignee = self._issue_assignee()
        if not issues or not assignee:
            return self.browse()
        issues.with_context(NO_MAIL_CTX).write({'user_id': assignee.id})
        issues.filtered(lambda i: i.state not in ('closed', 'skipped'))._schedule_assignee_activity(
            _('Performance issue'))
        _logger.info('perf_monitor: %d unassigned issue(s) assigned to %s', len(issues), assignee.login)
        return issues

    def _schedule_assignee_activity(self, summary):
        """One open To-Do activity per issue for its assignee (Odoo activity only: never an e-mail).
        Fail-safe: an assignee without access to the issue only gets a warning in the log."""
        for issue in self.sudo().filtered('user_id'):
            if issue.activity_ids.filtered(lambda a: a.user_id == issue.user_id):
                continue
            try:
                with self.env.cr.savepoint():
                    issue.with_context(NO_MAIL_CTX).activity_schedule(
                        'mail.mail_activity_data_todo', user_id=issue.user_id.id, summary=summary,
                        note=_('%(sev)s %(type)s: %(name)s', sev=dict(SEVERITIES).get(issue.severity),
                               type=dict(ISSUE_TYPES).get(issue.issue_type), name=issue.name))
            except Exception as exc:  # pylint: disable=broad-except
                _logger.warning('perf_monitor: no activity for issue %s (%s): %s', issue.id, issue.user_id.login, exc)

    def _message_auto_subscribe_notify(self, partner_ids, template):
        """Staging mail is off and the task is an activity: never send the "assigned to you" e-mail."""
        return

    # ------------------------------------------------------------------ EXPLAIN (local DB only)
    def _is_local_db(self):
        return self.env.cr.dbname in self.tag_ids.mapped('name')

    def _run_explain(self):
        """EXPLAIN (GENERIC_PLAN, FORMAT JSON) of the top SQL when it comes from this very DB
        (PostgreSQL >= 16; never ANALYZE, so the statement is not executed). Records the plan
        summary and the big tables read by a Seq Scan. Fail-safe."""
        self.ensure_one()
        sql = explainable_sql(self.top_sql)
        if not sql or not self._is_local_db() or self.env.cr._cnx.server_version < 160000:
            return False
        cr = self.env.cr
        cr.execute('SHOW statement_timeout')
        previous = cr.fetchone()[0]
        try:
            # a failing statement only rolls back to the savepoint (the SET LOCAL too): the transaction
            # stays usable and the real error is kept, not "current transaction is aborted"
            with cr.savepoint(flush=False), mute_logger('odoo.sql_db'):
                cr.execute('SET LOCAL statement_timeout = %s', (EXPLAIN_TIMEOUT_MS,))
                cr.execute('EXPLAIN (GENERIC_PLAN, FORMAT JSON) ' + sql)  # pylint: disable=sql-injection
                plan = cr.fetchone()[0]
                cr.execute('SET LOCAL statement_timeout = %s', (previous,))
        except Exception as exc:  # pylint: disable=broad-except
            error = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
            self.write({'explain_plan': 'EXPLAIN unavailable: %s' % error, 'seq_scan_tables': False,
                        'seq_scan_rows': 0})
            return False
        if isinstance(plan, str):
            plan = json.loads(plan)
        scanned = {n['Relation Name'] for n in plan_nodes(plan) if n.get('Node Type') == 'Seq Scan'
                   and n.get('Relation Name')}
        sizes = self._table_rows(scanned)
        big = sorted(((rows, t) for t, rows in sizes.items() if rows >= BIG_TABLE_ROWS), reverse=True)
        self.write({
            'explain_plan': plan_summary(plan),
            'seq_scan_tables': ', '.join('%s (%d rows)' % (t, rows) for rows, t in big) or False,
            'seq_scan_rows': big[0][0] if big else 0,
        })
        return True

    def _table_rows(self, tables):
        if not tables:
            return {}
        self.env.cr.execute("""
            SELECT c.relname, GREATEST(c.reltuples, 0)::bigint
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'm') AND c.relname = ANY(%s)
        """, (list(tables),))
        return dict(self.env.cr.fetchall())

    def _plan_tables(self):
        """Tables of the stored plan summary, else the FROM / JOIN targets of the top SQL."""
        self.ensure_one()
        found = re.findall(r'\bon ([a-z_][a-z0-9_]*)', self.explain_plan or '')
        if not found and self.top_sql:
            found = re.findall(r'\b(?:from|join)\s+"?([a-z_][a-z0-9_]*)"?', self.top_sql, re.I)
        return list(dict.fromkeys(t.lower() for t in found))[:8]

    # ------------------------------------------------------------------ actions
    def _check_manager(self):
        if not self.env.user.has_group('perf_monitor.group_perf_manager'):
            raise UserError(_('Only Perf Monitor Managers can change issues.'))

    def action_close(self):
        self._check_manager()
        self.write({'state': 'closed', 'closed_date': fields.Datetime.now(), 'closed_by': self.env.uid})
        self.sudo().activity_ids.unlink()  # dropped, not "done": marking done posts a message
        return True

    def action_open(self):
        self._check_manager()
        # reopening a skipped issue ends its skip rule
        self.filtered(lambda i: i.state == 'skipped').skip_rule_id.filtered('active').write({'active': False})
        self.write({'state': 'open'})
        return True

    def action_skip(self):
        self.ensure_one()
        self._check_manager()
        return {
            'type': 'ir.actions.act_window', 'name': _('Skip this slowness'), 'res_model': 'perf.issue.skip.wizard',
            'view_mode': 'form', 'target': 'new', 'context': {'default_issue_id': self.id},
        }

    def action_view_slow_calls(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('perf_monitor.action_perf_agent_slow_call_remote')
        action.update(domain=[('issue_id', '=', self.id)], context={'create': False})
        return action

    def action_refresh_explain(self):
        self._check_manager()
        for issue in self:
            issue._run_explain()
        self._update_severity()
        return True


class PerfAgentSlowCallRemote(models.Model):
    _inherit = 'perf.agent.slow.call.remote'

    issue_id = fields.Many2one('perf.issue', 'Issue', index=True, ondelete='set null', readonly=True)
    issue_checked = fields.Boolean(readonly=True, copy=False,
                                   help='Handled by the issue build (rules), with or without an issue')
    issue_key = fields.Char('Issue key', readonly=True, index=True, copy=False,
                            help='Key of the issue this slow call maps to (set by the issue build, also when no '
                                 'issue is made): counts the recurring slow calls')
    skip_rule_id = fields.Many2one('perf.skip.rule', 'Skipped by', readonly=True, index=True, ondelete='set null',
                                   copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        self.env.cr.cache.pop(CALL_GROUPS_MEMO, None)
        return super().create(vals_list)

    def write(self, vals):
        self.env.cr.cache.pop(CALL_GROUPS_MEMO, None)
        return super().write(vals)

    def unlink(self):
        self.env.cr.cache.pop(CALL_GROUPS_MEMO, None)
        return super().unlink()

    @api.model
    def _cron_purge(self):
        self.env.cr.cache.pop(CALL_GROUPS_MEMO, None)   # raw SQL delete
        return super()._cron_purge()

    def init(self):
        super().init()
        self.env.cr.execute('CREATE INDEX IF NOT EXISTS perf_agent_slow_call_remote_issue_todo_idx '
                            'ON perf_agent_slow_call_remote (id) WHERE issue_checked IS NOT TRUE')

    def _issue_values(self):
        self.ensure_one()
        return {f: self[f] for f in (
            'kind', 'duration_ms', 'sql_ms', 'tile_ms', 'tile_count', 'model_name', 'method', 'route', 'cron_name',
            'ui_dashboard', 'ui_tile', 'top_sql_fingerprint', 'top_sql_calls', 'top_sql_ms',
            'top_sql_callsite', 'error')}


def strip_sql(text):
    """Literal-free SQL for anything leaving this server (AI prompt)."""
    return redact_literals(redact_sql(text or '')) if text else ''


def is_testing():
    return getattr(threading.current_thread(), 'testing', False)
