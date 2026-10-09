# -*- coding: utf-8 -*-
"""In-agent collector: "Perf Monitor: agent collect" samples this host and this database and pushes the
metrics / Top SQL / live queries to the central perf_monitor (/perf_monitor/api/v1/push, the endpoint of
the root collector), with the token of perf_monitor_agent: no root script, no perf_monitor_ro role.

DB side through the Odoo cursor (Odoo DB user): SELECT 1 latency, pg_stat_activity, pg_stat_statements
(skipped when the extension is missing / unreadable: logged once per process and state change at INFO, and
reported to the central as payload key ``pgss`` = ok / missing / unreadable; nothing is ever installed).
Query texts are redacted (tools/perf_redact.py) before they leave the database. Host side (CPU / RAM / disk /
load / network) only when perf_monitor_agent.send_host_metrics is on: enable it in ONE database per host."""
import json
import logging
import threading
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone

import requests

from odoo import api, fields, models, release

from ..tools import collect
from ..tools.perf_redact import redact_sql
from .perf_agent_slow_call import PUSH_TIMEOUT

_logger = logging.getLogger(__name__)

PREFIX = 'perf_monitor_agent.'
COLLECT_PATH = '/perf_monitor/api/v1/push'
STATEMENT_TIMEOUT = '5s'
COLLECTOR_VERSION = 'perf_monitor_agent/%s'  # central: perf_monitor_agent* = 5-minute status windows
_FALSE = ('0', 'false', 'no', 'off')
# pg_stat_statements status sent to the central (payload key ``pgss``); an older agent sends nothing (= unknown)
PGSS_OK, PGSS_MISSING, PGSS_UNREADABLE = 'ok', 'missing', 'unreadable'
_PGSS_LOGGED = {}   # db name -> last status logged by this process: one INFO line per state change, not per run

LIVE_SQL = """
SELECT pid, usename, application_name, host(client_addr) AS client_addr, state,
       wait_event_type, wait_event,
       to_char(xact_start AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS xact_start,
       to_char(query_start AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS query_start,
       EXTRACT(EPOCH FROM (now() - CASE WHEN state LIKE 'idle in transaction%%'
                                        THEN state_change ELSE query_start END))::float8 AS duration_s,
       pg_blocking_pids(pid) AS blocking_pids,
       left(query, %(qmax)s) AS query
  FROM pg_stat_activity
 WHERE pid <> pg_backend_pid()
   AND datname = current_database()
   AND ((state = 'active' AND now() - query_start > make_interval(secs => %(long_s)s))
        OR wait_event_type = 'Lock'
        OR (state LIKE 'idle in transaction%%' AND now() - state_change > make_interval(secs => %(idle_s)s)))
 ORDER BY duration_s DESC NULLS LAST
 LIMIT %(lim)s
"""


def utcnow_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class PerfAgentCollector(models.Model):
    """Small key -> JSON state of the collector (previous CPU / network / pg_stat_statements counters),
    kept in the database because each cron run may happen in another worker process."""
    _name = 'perf.agent.collector'
    _description = 'Perf Monitor Agent: Collector state'
    _rec_name = 'key'

    key = fields.Char(required=True, readonly=True, index=True)
    data = fields.Text(readonly=True)

    _sql_constraints = [('key_uniq', 'unique(key)', 'One state per key.')]

    # ------------------------------------------------------------------ state
    @api.model
    def _get_state(self, key):
        rec = self.sudo().search([('key', '=', key)], limit=1)
        try:
            return json.loads(rec.data) if rec and rec.data else {}
        except ValueError:
            return {}

    @api.model
    def _set_state(self, key, value):
        rec = self.sudo().search([('key', '=', key)], limit=1)
        data = json.dumps(value, separators=(',', ':'))
        if rec:
            rec.data = data
        else:
            self.sudo().create({'key': key, 'data': data})

    # ------------------------------------------------------------------ config
    @api.model
    def _collect_config(self):
        """{enabled, collect, send_host}: kill switch perf_monitor_agent.enabled, collect_enabled and
        send_host_metrics (all default on)."""
        params = self.env['ir.config_parameter'].sudo()

        def flag(key):
            return (params.get_param(PREFIX + key) or '1').strip().lower() not in _FALSE
        return {'enabled': flag('enabled'), 'collect': flag('collect_enabled'),
                'send_host': flag('send_host_metrics')}

    # ------------------------------------------------------------------ DB sampling (Odoo cursor)
    @contextmanager
    def _stat_cursor(self):
        """Odoo cursor inside a savepoint with a short statement_timeout: a failing / slow statistics query
        is rolled back alone and never breaks the cron transaction."""
        cr = self.env.cr
        with cr.savepoint(flush=False):
            cr.execute('SHOW statement_timeout')
            previous = cr.fetchone()[0]
            cr.execute("SELECT set_config('statement_timeout', %s, true)", [STATEMENT_TIMEOUT])
            yield cr
            cr.execute("SELECT set_config('statement_timeout', %s, true)", [previous])

    def _dictfetchall(self, cr):
        cols = [d[0] for d in cr.description]
        return [dict(zip(cols, row)) for row in cr.fetchall()]

    @api.model
    def _db_metrics(self):
        """{db_latency_ms, pg_connections} (SELECT 1 round trip on the Odoo cursor)."""
        with self._stat_cursor() as cr:
            t0 = time.perf_counter()
            cr.execute('SELECT 1')
            cr.fetchone()
            latency = round((time.perf_counter() - t0) * 1000.0, 3)
            cr.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")
            return {'db_latency_ms': latency, 'pg_connections': int(cr.fetchone()[0])}

    @api.model
    def _pg_version(self):
        with self._stat_cursor() as cr:
            cr.execute('SELECT version()')
            return cr.fetchone()[0]

    @api.model
    def _live_queries(self, th, ts):
        with self._stat_cursor() as cr:
            cr.execute(LIVE_SQL, {'long_s': float(th['long_running_s']), 'idle_s': float(th['idle_in_tx_s']),
                                  'lim': int(th['live_max_rows']), 'qmax': int(th['live_query_max_chars'])})
            rows = self._dictfetchall(cr)
        for r in rows:
            r['ts'] = ts
            r['query'] = redact_sql(r.get('query') or '')
            r['blocking_pids'] = ','.join(str(p) for p in (r.get('blocking_pids') or []))
            r['category'] = collect.live_category(r)
            if r.get('duration_s') is not None:
                r['duration_s'] = round(float(r['duration_s']), 3)
        return rows

    @api.model
    def _statements_meta(self):
        """(quoted schema, column map, has showtext function) of pg_stat_statements, or None."""
        with self._stat_cursor() as cr:
            cr.execute("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_stat_statements'")
            row = cr.fetchone()
            if not row:
                return None
            schema = row[0]
            cr.execute("SELECT attname FROM pg_attribute WHERE attrelid = (%s || '.pg_stat_statements')::regclass "
                       "AND attnum > 0 AND NOT attisdropped", [schema])
            cols = {r[0] for r in cr.fetchall()}
            cr.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                       "WHERE n.nspname = %s AND p.proname = 'pg_stat_statements' "
                       "AND 'showtext' = ANY(p.proargnames)", [schema])
            func = bool(cr.fetchone()[0])
        new = 'total_exec_time' in cols
        return ('"%s"' % schema.replace('"', ''), {
            'total': 'total_exec_time' if new else 'total_time',
            'max': 'max_exec_time' if new else 'max_time',
        }, func)

    @api.model
    def _statements(self, meta, qmax):
        """Cumulative pg_stat_statements counters of this database, keyed "queryid:userid". The Odoo DB user
        runs the application queries itself: its statements are NOT excluded (unlike the root collector)."""
        schema, c, func = meta
        if func:
            source, text_col = '%s.pg_stat_statements(false)' % schema, ''
        else:
            source, text_col = '%s.pg_stat_statements' % schema, 'left(query, %s) AS query, '
        sql = ('SELECT queryid::text AS queryid, userid::bigint AS userid, ' + text_col + 'calls, '
               '%(total)s AS total_ms, %(max)s AS max_ms, rows, shared_blks_hit, shared_blks_read, '
               'temp_blks_read, temp_blks_written FROM %(source)s '
               'WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) '
               'AND queryid IS NOT NULL' % dict(c, source=source))
        with self._stat_cursor() as cr:
            cr.execute(sql, None if func else [int(qmax)])
            rows = self._dictfetchall(cr)
        current = {}
        for r in rows:
            key = '%s:%s' % (r['queryid'], r.get('userid'))
            if key in current:  # same queryid (toplevel variants): aggregate the counters
                for col in collect.SQL_COUNTERS:
                    current[key][col] = float(current[key].get(col) or 0) + float(r.get(col) or 0)
            else:
                current[key] = r
        return current

    @api.model
    def _statement_texts(self, meta, queryids, qmax):
        ids = sorted({int(q) for q in queryids})
        if not ids or not meta[2]:
            return {}
        with self._stat_cursor() as cr:
            cr.execute('SELECT queryid::text, left(query, %%s) FROM %s.pg_stat_statements(true) '
                       'WHERE queryid = ANY(%%s::bigint[]) '
                       'AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())' % meta[0],
                       [int(qmax), ids])
            rows = cr.fetchall()
        out = {}
        for qid, text in rows:
            if text and qid not in out:
                out[qid] = text
        return out

    @api.model
    def _log_pgss(self, status, detail=''):
        """One INFO line per process and per state change of pg_stat_statements in this database (the
        collect cron runs every 5 minutes: no repeated warning in the Odoo log)."""
        dbname = self.env.cr.dbname
        previous = _PGSS_LOGGED.get(dbname)
        if previous == status:
            return
        _PGSS_LOGGED[dbname] = status
        if status == PGSS_MISSING:
            _logger.info('perf_monitor_agent: pg_stat_statements extension not installed in %s: Top SQL off '
                         '(reported to perf_monitor; to enable it see the perf_monitor_agent README: '
                         'shared_preload_libraries + CREATE EXTENSION pg_stat_statements). Logged once.', dbname)
        elif status == PGSS_UNREADABLE:
            _logger.info('perf_monitor_agent: pg_stat_statements not readable in %s: Top SQL off (%s). '
                         'Logged once.', dbname, detail)
        elif previous is not None:
            _logger.info('perf_monitor_agent: pg_stat_statements readable again in %s: Top SQL on', dbname)

    @api.model
    def _top_sql(self, th, ts, states, info=None):
        """Top SQL since the previous run (the first run only stores the baseline). [] when
        pg_stat_statements is missing or unreadable (logged once, see _log_pgss). The new counters go to
        ``states['sql']``; ``info['pgss']`` gets the status sent to the central (ok / missing / unreadable)."""
        info = {} if info is None else info
        try:
            meta = self._statements_meta()
            if not meta:
                info['pgss'] = PGSS_MISSING
                self._log_pgss(PGSS_MISSING)
                return []
            current = self._statements(meta, th['query_max_chars'])
        except Exception as exc:
            info['pgss'] = PGSS_UNREADABLE
            self._log_pgss(PGSS_UNREADABLE, str(exc)[:300])
            return []
        info['pgss'] = PGSS_OK
        self._log_pgss(PGSS_OK)
        prev = self._get_state('sql')
        selected, states['sql'] = collect.compute_top_sql(prev, current, th, ts)
        if not prev:
            return []  # baseline
        missing = [r['queryid'] for r in selected if not r['query']]
        if missing:
            try:
                texts = self._statement_texts(meta, missing, th['query_max_chars'])
            except Exception as exc:
                _logger.warning('perf_monitor_agent: pg_stat_statements texts unavailable: %s', exc)
                texts = {}
            for r in selected:
                if not r['query']:
                    text = texts.get(r['queryid'])
                    if text:
                        r['query'] = redact_sql(text)[:int(th['query_max_chars'])]
                    else:
                        r['query_unavailable'] = True
        return selected

    # ------------------------------------------------------------------ payload
    @api.model
    def _build_payload(self, send_host, states=None):
        """Collector payload (version 1, as perf_collector.py). Every step fails soft. The new counters
        (host CPU / network, pg_stat_statements) are put in ``states``: saved once the push succeeded, so
        a failed push is covered by the next delta."""
        states = {} if states is None else states
        th = dict(collect.DEFAULT_THRESHOLDS)
        ts = utcnow_str()
        host = {'odoo_version': release.version}
        metric = {'ts': ts}
        if send_host:
            try:
                row, states['host'] = collect.HostSampler(sleep=self._sleep).sample(self._get_state('host'))
                metric.update(row)
            except Exception as exc:
                _logger.warning('perf_monitor_agent: host sample failed: %s', exc)
            try:
                host.update(collect.host_static_info())
                host.update(collect.fetch_imds())
            except Exception as exc:
                _logger.debug('perf_monitor_agent: host info failed: %s', exc)
        try:
            metric.update(self._db_metrics())
        except Exception as exc:
            _logger.warning('perf_monitor_agent: DB latency sample failed: %s', exc)
        try:
            host['pg_version'] = self._pg_version()
        except Exception as exc:
            _logger.debug('perf_monitor_agent: pg version unavailable: %s', exc)
        try:
            live = self._live_queries(th, ts)
        except Exception as exc:
            _logger.warning('perf_monitor_agent: live query sample failed: %s', exc)
            live = []
        info = {}
        sql = self._top_sql(th, ts, states, info)
        module = self.env.ref('base.module_perf_monitor_agent', raise_if_not_found=False)
        return {
            'version': 1, 'db_name': self.env.cr.dbname, 'sent_at': ts,
            'collector_version': COLLECTOR_VERSION % ((module and module.latest_version) or release.version),
            'host': host, 'metrics': [metric] if len(metric) > 1 else [], 'sql': sql, 'live': live,
            'pgss': info.get('pgss') or PGSS_UNREADABLE,
        }

    @api.model
    def _sleep(self, seconds):
        """Short second host sample (first run); no wait inside tests."""
        if not getattr(threading.current_thread(), 'testing', False):
            time.sleep(seconds)

    # ------------------------------------------------------------------ cron
    @api.model
    def _cron_collect_and_push(self):
        """"Perf Monitor: agent collect". Returns the central answer ({'accepted', 'rejected'}) or {} when
        skipped / failed (logged, never the token; never raises for a network / central error)."""
        cfg = self._collect_config()
        if not cfg['enabled'] or not cfg['collect']:
            _logger.debug('perf_monitor_agent: collect disabled')
            return {}
        push_cfg = self.env['perf.agent.slow.call']._valid_push_config('collect')
        if not push_cfg:
            return {}
        url, token, central_db = push_cfg
        states = {}
        payload = self._build_payload(cfg['send_host'], states)
        body = json.dumps(payload, separators=(',', ':'), default=str)
        with requests.Session() as session:
            session.headers['User-Agent'] = 'perf_monitor_agent/%s' % release.version
            query = ''
            if central_db:
                query = '?' + urllib.parse.urlencode({'db': central_db})
                try:  # pin the central DB in the session cookie (multi-DB central), like the collector
                    session.get('%s/web%s' % (url, query), timeout=PUSH_TIMEOUT, allow_redirects=False)
                except requests.RequestException as exc:
                    _logger.warning('perf_monitor_agent: central unreachable: %s', exc)
                    return {}
            try:
                resp = session.post(url + COLLECT_PATH + query, data=body, timeout=PUSH_TIMEOUT,
                                    allow_redirects=False,
                                    headers={'Content-Type': 'application/json', 'X-Perf-Token': token})
                result = resp.json() if resp.status_code == 200 else None
            except (requests.RequestException, ValueError) as exc:
                _logger.warning('perf_monitor_agent: collect push failed: %s', exc)
                return {}
        if not isinstance(result, dict) or not result.get('ok'):
            _logger.warning('perf_monitor_agent: collect push refused by central: HTTP %s %s',
                            resp.status_code, (resp.text or '')[:200])
            return {}
        for key, value in states.items():
            self._set_state(key, value)
        _logger.info('perf_monitor_agent: collected %s metric / %s sql / %s live rows (central: %s)',
                     len(payload['metrics']), len(payload['sql']), len(payload['live']),
                     {k: result.get(k) for k in ('accepted', 'rejected')})
        return {k: result.get(k) for k in ('accepted', 'rejected')}
