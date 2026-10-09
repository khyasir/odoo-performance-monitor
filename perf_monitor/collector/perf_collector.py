#!/usr/bin/env python3
"""Perf Monitor collector agent (Phase 1).

Single-file, lightweight agent that samples OS metrics, PostgreSQL statistics
views (pg_stat_activity / pg_stat_statements) and EC2 instance metadata, and
pushes them to the central Odoo ``perf_monitor`` module.

Dependencies: Python 3.6+ stdlib + psycopg2. psutil is optional (falls back to
/proc parsing). The collector never reads business tables: it only touches
pg_stat_* views, pg_extension, pg_stat_statements, version() and ``SELECT 1``.

Query text (pg_stat_activity.query and pg_stat_statements.query) can contain literal
values (customer names, amounts, passwords); it is passed through ``redact_sql()``
before it is queued, spooled or sent, so literals leave the host only as ``?``.
Only the redacted SQL shape, the counters and host/OS metrics are sent.
"""
import argparse
import configparser
import http.cookiejar
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:  # optional
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on host
    psutil = None

__version__ = "1.0.2"
APP_NAME = "perf_monitor_collector"
USER_AGENT = "perf-monitor-collector/%s" % __version__
_logger = logging.getLogger("perf_collector")

DEFAULTS = {
    "collector": {
        "central_url": "http://127.0.0.1:8069",
        "central_db": "",
        "token": "",
        "pg_dsn": "",
        "sample_interval": "60",
        "live_interval": "60",
        "topsql_interval": "300",
        "push_interval": "60",
        "spool_path": "/var/lib/perf-monitor/spool.db",
        "state_path": "/var/lib/perf-monitor/state.json",
        "spool_max_mb": "50",
        # spooled payloads pushed per cycle; keep below perf_monitor.max_pushes_per_minute (default 10)
        "flush_batch": "5",
        "http_timeout": "15",
        "backoff_max": "900",
        "disk_path": "/",
        "imds": "on",
        "log_level": "INFO",
    },
    "thresholds": {
        "long_running_s": "5",
        "idle_in_tx_s": "60",
        "slow_mean_ms": "500",
        "high_calls": "1000",
        "high_blks_read": "10000",
        "top_n": "20",
        "live_max_rows": "200",
        "query_max_chars": "4000",
        "live_query_max_chars": "2000",
    },
}


# --- BEGIN shared: redact_sql / is_allowed_central_url (keep identical in perf_collector.py) ---
_RX_WS = re.compile(r"\s+")
_RX_IDENT = re.compile(r"[^\W\d][\w$]*")
_RX_NUMBER = re.compile(
    r"(?:0[xX][0-9A-Fa-f_]+|0[oO][0-7_]+|0[bB][01_]+"
    r"|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9]+)?)\w*")
_RX_PARAM = re.compile(r"\$[0-9]+")
_RX_PYPARAM = re.compile(r"%(?:\([^)]*\))?s")
_RX_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")
_RX_COMMENT_MARK = re.compile(r"/\*|\*/")
_STRING_PREFIXES = ("E", "B", "X", "N")
_LITERAL_WORDS = ("NULL", "TRUE", "FALSE", "DEFAULT")
_SAFE_CENTRAL_URL = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+(/[A-Za-z0-9._\-/]*)?$")
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _skip_string(text, i, backslash):
    """Index just after the single-quoted string opening at text[i] (len(text) if unterminated)."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def redact_sql(text):
    """Replace literal values in SQL text by ``?``; keep identifiers, keywords and placeholders.

    Single pass tokenizer. Redacted: '...' strings ('' escapes), E'...' (backslash escapes), U&'...',
    B'...', X'...', N'...', $$...$$ / $tag$...$tag$ bodies, numeric literals (not digits inside
    identifiers such as foo1 or t2.col; $1 / %s / %(name)s placeholders are kept), IN (<literals or
    placeholders>) -> IN (?) (IN (SELECT ...) is kept), and everything after the keyword PASSWORD
    -> ``PASSWORD ?``. Double-quoted identifiers are kept; comment bodies are dropped (``--?``,
    ``/*?*/``; nested block comments are followed to the matching ``*/``). Truncated input is
    handled: an unterminated string / dollar quote / comment is redacted up to the end.
    """
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    out = []
    # one frame per open parenthesis: [is_in_list, out index of "(", only_literals, has_literal]
    stack = []
    prev_word = None  # last significant token when it is a word (upper case), for "IN ("
    cast = False  # inside "::type name" (type words count as literals within IN lists)
    n = len(text)
    i = 0

    def mark(kind):
        if stack and stack[-1][0]:
            if kind == "lit":
                stack[-1][3] = True
            elif kind == "other":
                stack[-1][2] = False

    while i < n:
        c = text[i]
        if c.isspace():
            m = _RX_WS.match(text, i)
            out.append(m.group())
            i = m.end()
            continue
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            out.append("--?")
            continue
        if c == "/" and text.startswith("/*", i):
            # PostgreSQL block comments nest: skip until the depth is back to 0 (or the end)
            depth, i = 1, i + 2
            while depth:
                m = _RX_COMMENT_MARK.search(text, i)
                if not m:
                    i = n
                    break
                depth += 1 if m.group() == "/*" else -1
                i = m.end()
            out.append("/*?*/")
            continue
        if c == "'":
            i = _skip_string(text, i, False)
            out.append("?")
            mark("lit")
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            mark("lit" if cast else "other")
        elif c == "$":
            m = _RX_PARAM.match(text, i) or _RX_DOLLAR_TAG.match(text, i)
            if m and "0" <= m.group()[1:2] <= "9":
                out.append(m.group())
                i = m.end()
                mark("lit")
            elif m:
                j = text.find(m.group(), m.end())
                i = n if j < 0 else j + len(m.group())
                out.append("?")
                mark("lit")
            else:
                out.append(c)
                i += 1
                mark("other")
        elif "0" <= c <= "9" or (c == "." and i + 1 < n and "0" <= text[i + 1] <= "9"):
            m = _RX_NUMBER.match(text, i)
            out.append("?")
            i = m.end()
            mark("lit")
        elif c == "%" and _RX_PYPARAM.match(text, i):
            m = _RX_PYPARAM.match(text, i)
            out.append(m.group())
            i = m.end()
            mark("lit")
        elif c == "?":
            out.append(c)
            i += 1
            mark("lit")
        elif _RX_IDENT.match(text, i):
            m = _RX_IDENT.match(text, i)
            j = m.end()
            upper = m.group().upper()
            if j < n and text[j] == "'" and upper in _STRING_PREFIXES:
                i = _skip_string(text, j, upper == "E")
                out.append("?")
                mark("lit")
            elif upper == "U" and text.startswith("&'", j):
                i = _skip_string(text, j + 1, False)
                out.append("?")
                mark("lit")
            elif upper == "PASSWORD":
                out.append("PASSWORD ?")
                return "".join(out)
            else:
                out.append(m.group())
                i = j
                mark("lit" if cast or upper in _LITERAL_WORDS else "other")
                prev_word = upper
                continue  # a word keeps a pending cast active ("::character varying")
        elif c == "(":
            mark("other")
            stack.append([prev_word == "IN", len(out), True, False])
            out.append(c)
            i += 1
        elif c == ")":
            frame = stack.pop() if stack else None
            if frame and frame[0] and frame[2] and frame[3]:
                del out[frame[1]:]
                out.append("(?)")
            else:
                out.append(c)
            i += 1
        elif c == ":" and text.startswith("::", i):
            out.append("::")
            i += 2
            prev_word = None
            cast = True
            continue
        else:
            out.append(c)
            i += 1
            if c not in ",+-":
                mark("other")
        prev_word = None
        cast = False
    return "".join(out)


def is_allowed_central_url(url):
    """https:// anywhere; plain http:// only for the loopback hosts 127.0.0.1, [::1] and localhost."""
    if not isinstance(url, str) or not _SAFE_CENTRAL_URL.match(url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    if not host:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in _LOOPBACK_HOSTS
# --- END shared ---


def utcnow_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_config(path=None):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    if path:
        if not cp.read(path):
            raise SystemExit("cannot read config file %s" % path)
    cfg = {k: v for k, v in cp["collector"].items()}
    cfg["thresholds"] = {k: float(v) for k, v in cp["thresholds"].items()}
    for key in ("sample_interval", "live_interval", "topsql_interval", "push_interval",
                "spool_max_mb", "flush_batch", "http_timeout", "backoff_max"):
        cfg[key] = float(cfg[key])
    cfg["central_url"] = cfg["central_url"].rstrip("/")
    cfg["imds"] = cfg["imds"].lower() in ("1", "on", "true", "yes")
    if os.environ.get("PERF_PG_DSN"):
        cfg["pg_dsn"] = os.environ["PERF_PG_DSN"]
    return cfg


# ---------------------------------------------------------------------------
# OS metrics
# ---------------------------------------------------------------------------
def parse_proc_stat(text):
    """Return (total, idle, iowait) jiffies of the aggregate cpu line."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            vals = [int(v) for v in line.split()[1:]]
            vals += [0] * (8 - len(vals))
            # user nice system idle iowait irq softirq steal (guest* already in user)
            total = sum(vals[:8])
            return total, vals[3], vals[4]
    raise ValueError("no cpu line in /proc/stat")


def parse_meminfo(text):
    """Return dict of meminfo values in kB."""
    out = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        fields = parts[1].split()
        if fields:
            try:
                out[parts[0].strip()] = int(fields[0])
            except ValueError:
                pass
    return out


def parse_loadavg(text):
    parts = text.split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def parse_net_dev(text):
    """Return (rx_bytes, tx_bytes) summed over all non-loopback interfaces."""
    rx = tx = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo" or not data.split():
            continue
        cols = data.split()
        if len(cols) < 9:
            continue
        rx += int(cols[0])
        tx += int(cols[8])
    return rx, tx


def _read(path):
    with open(path) as fh:
        return fh.read()


class OsSampler:
    """Samples CPU/RAM/disk/net. Rates are computed against the previous call."""

    def __init__(self, disk_path="/", proc_root="/proc", use_psutil=True, clock=time.monotonic):
        self.disk_path = disk_path
        self.proc_root = proc_root
        self.psutil = psutil if use_psutil else None
        self.clock = clock
        self._prev_cpu = None
        self._prev_net = None
        if self.psutil:
            self.psutil.cpu_percent(interval=None)
            self.psutil.cpu_times_percent(interval=None)

    def _proc(self, name):
        return _read(os.path.join(self.proc_root, name))

    def _cpu(self):
        if self.psutil:
            cpu = self.psutil.cpu_percent(interval=None)
            iowait = getattr(self.psutil.cpu_times_percent(interval=None), "iowait", 0.0)
            return cpu, iowait
        total, idle, iowait = parse_proc_stat(self._proc("stat"))
        prev, self._prev_cpu = self._prev_cpu, (total, idle, iowait)
        if not prev or total <= prev[0]:
            return None, None
        dt = float(total - prev[0])
        busy = dt - (idle - prev[1]) - (iowait - prev[2])
        return round(100.0 * busy / dt, 2), round(100.0 * (iowait - prev[2]) / dt, 2)

    def _mem(self):
        if self.psutil:
            vm, sw = self.psutil.virtual_memory(), self.psutil.swap_memory()
            total = vm.total / 1048576.0
            used = (vm.total - vm.available) / 1048576.0
            return total, used, sw.used / 1048576.0
        mi = parse_meminfo(self._proc("meminfo"))
        total = mi.get("MemTotal", 0) / 1024.0
        avail = mi.get("MemAvailable", mi.get("MemFree", 0) + mi.get("Cached", 0) + mi.get("Buffers", 0)) / 1024.0
        swap = (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) / 1024.0
        return total, total - avail, swap

    def _load(self):
        if hasattr(os, "getloadavg"):
            try:
                return os.getloadavg()
            except OSError:
                pass
        return parse_loadavg(self._proc("loadavg"))

    def _net(self):
        if self.psutil:
            counters = self.psutil.net_io_counters(pernic=True)
            rx = sum(c.bytes_recv for n, c in counters.items() if n != "lo")
            tx = sum(c.bytes_sent for n, c in counters.items() if n != "lo")
        else:
            rx, tx = parse_net_dev(self._proc("net/dev"))
        now = self.clock()
        prev, self._prev_net = self._prev_net, (now, rx, tx)
        if not prev or now <= prev[0] or rx < prev[1] or tx < prev[2]:
            return None, None
        dt = now - prev[0]
        return round((rx - prev[1]) * 8 / 1000.0 / dt, 2), round((tx - prev[2]) * 8 / 1000.0 / dt, 2)

    def _disk(self):
        st = os.statvfs(self.disk_path)
        total = st.f_blocks * st.f_frsize / 1073741824.0
        free = st.f_bavail * st.f_frsize / 1073741824.0
        used = (st.f_blocks - st.f_bfree) * st.f_frsize / 1073741824.0
        # df semantics: used / (used + available to non-root)
        pct = 100.0 * used / (used + free) if (used + free) else 0.0
        return total, used, pct

    def sample(self):
        row = {"ts": utcnow_str()}
        for name, fn in (("cpu", self._cpu), ("mem", self._mem), ("load", self._load),
                         ("net", self._net), ("disk", self._disk)):
            try:
                res = fn()
            except Exception as exc:  # never crash on a missing counter
                _logger.debug("os sample %s failed: %s", name, exc)
                continue
            if name == "cpu":
                row["cpu_pct"], row["iowait_pct"] = res
            elif name == "mem":
                total, used, swap = res
                row.update(ram_total_mb=round(total, 1), ram_used_mb=round(used, 1),
                           ram_pct=round(100.0 * used / total, 2) if total else None,
                           swap_used_mb=round(swap, 1))
            elif name == "load":
                row["load1"], row["load5"], row["load15"] = [round(x, 2) for x in res]
            elif name == "net":
                row["net_rx_kbps"], row["net_tx_kbps"] = res
            elif name == "disk":
                row["disk_total_gb"], row["disk_used_gb"], row["disk_pct"] = [round(x, 2) for x in res]
        return row


def host_static_info():
    info = {"hostname": socket.gethostname(), "vcpu": os.cpu_count()}
    try:
        if psutil:
            info["ram_mb"] = int(psutil.virtual_memory().total / 1048576)
        else:
            info["ram_mb"] = int(parse_meminfo(_read("/proc/meminfo")).get("MemTotal", 0) / 1024)
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# EC2 IMDSv2
# ---------------------------------------------------------------------------
IMDS_BASE = "http://169.254.169.254/latest"
_IMDS_KEYS = {
    "instance_type": "meta-data/instance-type",
    "instance_id": "meta-data/instance-id",
    "az": "meta-data/placement/availability-zone",
    "region": "meta-data/placement/region",
}


def fetch_imds(base=IMDS_BASE, timeout=1.0):
    """Query EC2 IMDSv2; returns dict with 'unknown' values when unavailable."""
    out = {k: "unknown" for k in _IMDS_KEYS}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(base + "/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})
        token = opener.open(req, timeout=timeout).read().decode()
    except Exception as exc:
        _logger.info("EC2 IMDS unavailable (%s); instance type unknown", exc)
        return out
    for key, path in _IMDS_KEYS.items():
        try:
            req = urllib.request.Request("%s/%s" % (base, path),
                                         headers={"X-aws-ec2-metadata-token": token})
            out[key] = opener.open(req, timeout=timeout).read().decode().strip() or "unknown"
        except Exception as exc:
            _logger.debug("IMDS %s failed: %s", path, exc)
    return out


# ---------------------------------------------------------------------------
# PostgreSQL (read-only, statistics views only)
# ---------------------------------------------------------------------------
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


class PgClient:
    """One persistent autocommit read-only connection, reconnected on failure."""

    def __init__(self, dsn, connect=None):
        self.dsn = dsn
        self._connect = connect
        self.conn = None
        self._pss = None  # (schema, column map) or False

    def _connector(self):
        if self._connect is None:
            import psycopg2  # imported lazily so tests/dry-run without DB work
            self._connect = psycopg2.connect
        return self._connect

    def ensure(self):
        if self.conn is not None and not getattr(self.conn, "closed", 0):
            return self.conn
        conn = self._connector()(self.dsn, application_name=APP_NAME, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cr:
            cr.execute("SET application_name = 'perf_monitor_collector'")
            cr.execute("SET statement_timeout = '5s'")
            cr.execute("SET default_transaction_read_only = on")
        self.conn, self._pss = conn, None
        return conn

    def reset(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def _run(self, sql, params=None, fetch="all"):
        conn = self.ensure()
        try:
            with conn.cursor() as cr:
                cr.execute(sql, params)
                if fetch == "one":
                    return cr.fetchone()
                cols = [d[0] for d in cr.description]
                return [dict(zip(cols, r)) for r in cr.fetchall()]
        except Exception:
            self.reset()
            raise

    def latency_ms(self):
        self.ensure()  # do not time the connection setup
        t0 = time.perf_counter()
        self._run("SELECT 1", fetch="one")
        return round((time.perf_counter() - t0) * 1000.0, 3)

    def connections(self):
        return int(self._run("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'",
                             fetch="one")[0])

    def pg_version(self):
        return self._run("SELECT version()", fetch="one")[0]

    def _statements_meta(self):
        """(schema, column map, has_function) of pg_stat_statements, or False when not installed.

        has_function: the extension exposes ``pg_stat_statements(showtext boolean)`` (ext >= 1.2), so
        the counters can be read without the query texts (no read of the external text file)."""
        if self._pss is None:
            row = self._run("SELECT extnamespace::regnamespace::text FROM pg_extension "
                            "WHERE extname = 'pg_stat_statements'", fetch="one")
            if not row:
                self._pss = False
            else:
                cols = {r["attname"] for r in self._run(
                    "SELECT attname FROM pg_attribute WHERE attrelid = (%s || '.pg_stat_statements')::regclass "
                    "AND attnum > 0 AND NOT attisdropped", (row[0],))}
                func = self._run(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = %s AND p.proname = 'pg_stat_statements' "
                    "AND 'showtext' = ANY(p.proargnames)", (row[0],), fetch="one")
                new = "total_exec_time" in cols
                self._pss = (row[0], {
                    "total": "total_exec_time" if new else "total_time",
                    "mean": "mean_exec_time" if new else "mean_time",
                    "max": "max_exec_time" if new else "max_time",
                }, bool(func and func[0]))
        return self._pss

    @staticmethod
    def _quoted_schema(schema):
        return '"%s"' % schema.replace('"', '')

    def statements(self, qmax=4000):
        """Cumulative pg_stat_statements counters of the current database (the collector's own role
        excluded). Rows have no "query" key when the showtext=false function form is available (the
        texts of the selected statements are then fetched by statement_texts()); with an old extension
        the view is read and "query" holds the text cut to ``qmax``. None when not installed."""
        meta = self._statements_meta()
        if not meta:
            return None
        schema, c, func = meta
        if func:
            source, text_col = "%s.pg_stat_statements(false)" % self._quoted_schema(schema), ""
        else:
            source, text_col = "%s.pg_stat_statements" % self._quoted_schema(schema), "left(query, %s) AS query, "
        sql = ("SELECT queryid::text AS queryid, userid::bigint AS userid, " + text_col + "calls, "
               "%(total)s AS total_ms, %(max)s AS max_ms, rows, shared_blks_hit, shared_blks_read, "
               "temp_blks_read, temp_blks_written FROM %(source)s "
               "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
               "AND queryid IS NOT NULL "
               # exclude the collector's own statements (its role), so it never reports itself
               "AND userid <> (SELECT oid FROM pg_roles WHERE rolname = current_user)"
               % dict(c, source=source))
        return self._run(sql, (int(qmax),) if not func else None)

    def statement_texts(self, queryids, qmax=4000, timeout_s=10):
        """{queryid (str): text} for ``queryids`` (function form only), in a short read-only transaction
        with its own statement_timeout (reading the text file can block behind the pgss garbage
        collection). Raises on failure; the connection is then dropped as for any other error."""
        meta = self._statements_meta()
        ids = sorted({int(q) for q in queryids})
        if not meta or not meta[2] or not ids:
            return {}
        sql = ("SELECT queryid::text AS queryid, left(query, %%s) AS query "
               "FROM %s.pg_stat_statements(true) WHERE queryid = ANY(%%s::bigint[]) "
               "AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())"
               % self._quoted_schema(meta[0]))
        conn = self.ensure()
        try:
            with conn.cursor() as cr:
                cr.execute("BEGIN")
                cr.execute("SET LOCAL statement_timeout = '%ds'" % int(timeout_s))
                cr.execute(sql, (int(qmax), ids))
                rows = cr.fetchall()
                cr.execute("COMMIT")
        except Exception:
            self.reset()
            raise
        out = {}
        for qid, text in rows:
            if text and str(qid) not in out:
                out[str(qid)] = text
        return out

    def live(self, long_s=5, idle_s=60, limit=200, qmax=2000):
        rows = self._run(LIVE_SQL, {"long_s": float(long_s), "idle_s": float(idle_s),
                                    "lim": int(limit), "qmax": int(qmax)})
        for r in rows:
            r["query"] = redact_sql(r.get("query") or "")
            r["blocking_pids"] = ",".join(str(p) for p in (r.get("blocking_pids") or []))
            if r.get("wait_event_type") == "Lock":
                r["category"] = "lock_wait"
            elif (r.get("state") or "").startswith("idle in transaction"):
                r["category"] = "idle_in_tx"
            else:
                r["category"] = "long_running"
            if r.get("duration_s") is not None:
                r["duration_s"] = round(float(r["duration_s"]), 3)
        return rows


# ---------------------------------------------------------------------------
# Top SQL delta computation (pure, unit tested)
# ---------------------------------------------------------------------------
QTEXT_CACHE_MAX = 500
QTEXT_TIMEOUT_S = 10
_COUNTERS = ("calls", "total_ms", "rows", "shared_blks_hit", "shared_blks_read",
             "temp_blks_read", "temp_blks_written")


def compute_top_sql(prev, current, th, ts=None):
    """Compute interval deltas vs ``prev`` and select the top statements.

    prev/current: {key: row}; key = "queryid:userid". Returns (selected, new_state).
    Rows may come without "query" (counters-only read): the selected rows then have query "" and the
    caller attaches the texts afterwards.
    A negative delta (stats reset / entry evicted) means the current value is the delta.
    """
    ts = ts or utcnow_str()
    new_state = {}
    candidates = []
    for key, cur in current.items():
        new_state[key] = {c: float(cur.get(c) or 0) for c in _COUNTERS}
        old = prev.get(key)
        delta = {}
        reset = old is None or float(cur.get("calls") or 0) < old["calls"] \
            or float(cur.get("total_ms") or 0) < old["total_ms"]
        for c in _COUNTERS:
            v = float(cur.get(c) or 0)
            delta[c] = v if reset else v - old[c]
        if delta["calls"] <= 0:
            continue
        mean = delta["total_ms"] / delta["calls"]
        reasons = []
        if mean > th.get("slow_mean_ms", 500):
            reasons.append("slow_mean")
        if delta["calls"] >= th.get("high_calls", 1000):
            reasons.append("high_calls")
        if delta["shared_blks_read"] >= th.get("high_blks_read", 10000):
            reasons.append("io")
        if delta["temp_blks_read"] + delta["temp_blks_written"] > 0:
            reasons.append("temp")
        if not reasons:
            continue
        cum_calls = float(cur.get("calls") or 0)
        candidates.append({
            "ts": ts,
            "queryid": str(cur.get("queryid")),
            "query": redact_sql(cur.get("query") or "")[: int(th.get("query_max_chars", 4000))],
            "calls": int(delta["calls"]),
            "total_ms": round(delta["total_ms"], 3),
            "mean_ms": round(mean, 3),
            "max_ms": round(float(cur.get("max_ms") or 0), 3),
            "rows": int(delta["rows"]),
            "shared_blks_hit": int(delta["shared_blks_hit"]),
            "shared_blks_read": int(delta["shared_blks_read"]),
            "temp_blks_read": int(delta["temp_blks_read"]),
            "temp_blks_written": int(delta["temp_blks_written"]),
            "cum_mean_ms": round(float(cur.get("total_ms") or 0) / cum_calls, 3) if cum_calls else 0.0,
            "reason": ",".join(reasons),
        })
    candidates.sort(key=lambda r: r["total_ms"], reverse=True)
    return candidates[: int(th.get("top_n", 20))], new_state


# ---------------------------------------------------------------------------
# Spool (sqlite) with size cap, oldest dropped first
# ---------------------------------------------------------------------------
class Spool:
    def __init__(self, path, max_mb=50.0):
        self.path = path
        self.max_bytes = int(max_mb * 1048576)
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS spool (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "created TEXT NOT NULL, body TEXT NOT NULL)")

    def size_bytes(self):
        return int(self.db.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM spool").fetchone()[0])

    def count(self):
        return int(self.db.execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def add(self, payload):
        body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"), default=str)
        if len(body) > self.max_bytes:
            _logger.warning("payload of %d bytes larger than spool cap, dropped", len(body))
            return 0
        self.db.execute("INSERT INTO spool (created, body) VALUES (?, ?)", (utcnow_str(), body))
        return self.enforce_cap()

    def enforce_cap(self):
        dropped = 0
        size = self.size_bytes()
        while size > self.max_bytes:
            row = self.db.execute("SELECT id, LENGTH(body) FROM spool ORDER BY id LIMIT 1").fetchone()
            if not row:
                break
            self.db.execute("DELETE FROM spool WHERE id = ?", (row[0],))
            size -= row[1]
            dropped += 1
        if dropped:
            _logger.warning("spool cap reached: dropped %d oldest payload(s)", dropped)
        return dropped

    def oldest(self, limit):
        return self.db.execute("SELECT id, body FROM spool ORDER BY id LIMIT ?", (int(limit),)).fetchall()

    def delete(self, spool_id):
        self.db.execute("DELETE FROM spool WHERE id = ?", (spool_id,))

    def close(self):
        self.db.close()


# ---------------------------------------------------------------------------
# Central client (urllib + cookie jar pinning the Odoo DB)
# ---------------------------------------------------------------------------
class PushError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status

    @property
    def permanent(self):
        """4xx other than 401 (token), 404 (DB routing) and 429 (throttled): retrying the same
        payload can never succeed, so it is dropped instead of blocking the spool forever."""
        return self.status is not None and 400 <= self.status < 500 and self.status not in RETRY_4XX


RETRY_4XX = (401, 404, 429)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow only same-origin redirects (e.g. /web?db= -> /web/login): a redirect to another scheme,
    host or port, or to a plain-http non-loopback URL, is refused (it would carry X-Perf-Token)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        target = "%s://%s" % (parts.scheme, parts.netloc)
        if ((parts.scheme, parts.netloc.lower()) != (old.scheme, old.netloc.lower())
                or not is_allowed_central_url(target)):
            raise urllib.error.HTTPError(newurl, code, "refusing redirect to %s" % target, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CentralClient:
    PUSH_PATH = "/perf_monitor/api/v1/push"

    def __init__(self, central_url, central_db, token, timeout=15.0):
        self.url = (central_url or "").rstrip("/")
        if not is_allowed_central_url(self.url):
            raise ValueError("central_url must be https:// (plain http:// only for 127.0.0.1, [::1] or "
                             "localhost): %r" % self.url)
        self.db = central_db
        self.token = token
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  _SafeRedirectHandler())
        self.pinned = False

    def pin_db(self):
        """GET /web?db=<db> so that Odoo stores the DB in our session cookie."""
        if not self.db:
            self.pinned = True
            return
        url = "%s/web?%s" % (self.url, urllib.parse.urlencode({"db": self.db}))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            self.opener.open(req, timeout=self.timeout).read()
        except urllib.error.HTTPError as exc:  # login page etc. still sets the cookie
            _logger.debug("pin db got HTTP %s", exc.code)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PushError("central unreachable: %s" % exc)
        self.pinned = True

    def push(self, body, _retry=True):
        if not self.pinned:
            self.pin_db()
        data = body.encode() if isinstance(body, str) else body
        qs = "?" + urllib.parse.urlencode({"db": self.db}) if self.db else ""
        req = urllib.request.Request(self.url + self.PUSH_PATH + qs, data=data, method="POST", headers={
            "Content-Type": "application/json", "X-Perf-Token": self.token, "User-Agent": USER_AGENT})
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            result = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and _retry and self.db:
                _logger.info("push got 404, re-pinning database %s", self.db)
                self.pinned = False
                return self.push(body, _retry=False)
            raise PushError("HTTP %s from central" % exc.code, exc.code)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PushError("central unreachable: %s" % exc)
        if not result.get("ok"):
            raise PushError("central refused payload: %s" % result.get("error"))
        if result.get("rejected"):
            _logger.warning("central rejected %s row(s) (timestamp outside the accepted window or invalid)",
                            result.get("rejected"))
        return int(result.get("accepted", 0))


# ---------------------------------------------------------------------------
# Main collector loop
# ---------------------------------------------------------------------------
class Collector:
    def __init__(self, cfg, pg=None, central=None, sampler=None, spool=None, imds_fn=fetch_imds,
                 clock=time.monotonic):
        self.cfg = cfg
        self.th = cfg["thresholds"]
        self.pg = pg or (PgClient(cfg["pg_dsn"]) if cfg.get("pg_dsn") else None)
        self.central = central or CentralClient(cfg["central_url"], cfg.get("central_db"), cfg["token"],
                                                cfg["http_timeout"])
        self.sampler = sampler or OsSampler(cfg.get("disk_path", "/"))
        self._spool = spool
        self.imds_fn = imds_fn
        self.clock = clock
        self.stop_event = threading.Event()
        self.metrics, self.sql, self.live = [], [], []
        self._host = None
        self._next = {}
        self._fail_count = 0
        self._push_after = 0.0
        self._spool_pending = False  # spooled payloads left after the last successful push
        self._sql_state = None
        self._qtext = {}  # queryid -> redacted text (LRU, oldest first), persisted in state.json
        self.pgss = None  # pg_stat_statements: "ok" / "missing" / "unreadable" (payload "pgss"), None: no DB

    @property
    def spool(self):
        if self._spool is None:
            self._spool = Spool(self.cfg["spool_path"], self.cfg["spool_max_mb"])
        return self._spool

    # -- host info (cached) --
    def host_info(self):
        if self._host is None:
            info = host_static_info()
            info.update(self.imds_fn() if self.cfg.get("imds", True) else
                        {k: "unknown" for k in _IMDS_KEYS})
            self._host = info
        info = dict(self._host)
        if self.pg and "pg_version" not in self._host:
            try:
                self._host["pg_version"] = info["pg_version"] = self.pg.pg_version()
            except Exception as exc:
                _logger.warning("pg version unavailable: %s", exc)
        return info

    # -- collection steps --
    def collect_os(self):
        row = self.sampler.sample()
        if self.pg:
            try:
                row["db_latency_ms"] = self.pg.latency_ms()
                row["pg_connections"] = self.pg.connections()
            except Exception as exc:
                _logger.warning("db sample failed: %s", exc)
        self.metrics.append(row)

    def collect_live(self):
        if not self.pg:
            return
        try:
            rows = self.pg.live(self.th["long_running_s"], self.th["idle_in_tx_s"],
                                self.th["live_max_rows"], self.th["live_query_max_chars"])
        except Exception as exc:
            _logger.warning("live query sample failed: %s", exc)
            return
        ts = utcnow_str()
        for r in rows:
            r["ts"] = ts
        self.live.extend(rows)

    def _load_sql_state(self):
        if self._sql_state is None:
            self._sql_state = {}
            try:
                with open(self.cfg["state_path"]) as fh:
                    data = json.load(fh)
                self._sql_state = data.get("sql", {})
                qtext = data.get("qtext") or {}
                if isinstance(qtext, dict):
                    self._qtext = {str(k): str(v) for k, v in qtext.items()}
            except (OSError, ValueError, AttributeError):
                pass
        return self._sql_state

    def _save_sql_state(self):
        path = self.cfg.get("state_path")
        if not path:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"sql": self._sql_state, "qtext": self._qtext, "saved": utcnow_str()}, fh)
            os.replace(tmp, path)
        except OSError as exc:
            _logger.debug("cannot persist sql state: %s", exc)

    def _attach_texts(self, selected):
        """Fill "query" of the selected rows from the bounded LRU text cache; fetch only the missing
        texts (one query). On failure the rows keep query "" + query_unavailable, one warning is
        logged and the texts are retried on the next cycle (still cache misses)."""
        qmax = int(self.th["query_max_chars"])
        missing = [r["queryid"] for r in selected if r["queryid"] not in self._qtext]
        if missing:
            try:
                fetched = self.pg.statement_texts(missing, qmax, QTEXT_TIMEOUT_S)
            except Exception as exc:
                _logger.warning("pg_stat_statements query text fetch failed for %d statement(s), rows sent "
                                "without text (retried next cycle): %s", len(missing), exc)
                fetched = {}
            for qid, text in fetched.items():
                self._qtext[qid] = redact_sql(text)[:qmax]
        for r in selected:
            text = self._qtext.pop(r["queryid"], None)
            if text is None:
                r["query"], r["query_unavailable"] = "", True
            else:
                self._qtext[r["queryid"]] = r["query"] = text  # re-insert: most recently used last
        while len(self._qtext) > QTEXT_CACHE_MAX:
            del self._qtext[next(iter(self._qtext))]

    def _set_pgss(self, status, detail=""):
        """Status of pg_stat_statements, sent to the central (payload "pgss"); logged once per change at
        INFO (not every cycle). Nothing is ever installed."""
        previous, self.pgss = self.pgss, status
        if previous == status:
            return
        if status == "missing":
            _logger.info("pg_stat_statements extension not installed: Top SQL off (reported to perf_monitor; "
                         "see the perf_monitor README to enable it). Logged once.")
        elif status == "unreadable":
            _logger.info("pg_stat_statements not readable: Top SQL off (%s). Logged once.", detail)
        elif previous is not None:
            _logger.info("pg_stat_statements readable again: Top SQL on")

    def collect_topsql(self):
        if not self.pg:
            return
        try:
            rows = self.pg.statements(self.th["query_max_chars"])
        except Exception as exc:
            self._set_pgss("unreadable", str(exc)[:300])
            return
        if rows is None:
            self._set_pgss("missing")
            return
        self._set_pgss("ok")
        current, has_text = {}, False
        for r in rows:
            has_text = has_text or "query" in r
            key = "%s:%s" % (r["queryid"], r.get("userid"))
            if key in current:  # same queryid (toplevel variants): aggregate counters
                for c in _COUNTERS:
                    current[key][c] = float(current[key].get(c) or 0) + float(r.get(c) or 0)
            else:
                current[key] = dict(r)
        prev = self._load_sql_state()
        first = not prev
        selected, self._sql_state = compute_top_sql(prev, current, self.th)
        if selected and not first and not has_text:  # counters-only read: texts of the selection only
            self._attach_texts(selected)
        self._save_sql_state()
        if not first:  # first snapshot is only a baseline
            self.sql.extend(selected)

    # -- payload / push --
    def build_payload(self):
        payload = {"version": 1, "collector_version": __version__, "sent_at": utcnow_str(),
                   "host": self.host_info(), "metrics": self.metrics, "sql": self.sql, "live": self.live}
        if self.pgss:
            payload["pgss"] = self.pgss
        self.metrics, self.sql, self.live = [], [], []
        return payload

    def _backoff(self):
        self._fail_count += 1
        delay = min(self.cfg["backoff_max"], self.cfg["push_interval"] * (2 ** (self._fail_count - 1)))
        self._push_after = self.clock() + delay
        return delay

    @staticmethod
    def _drop_rejected(body, exc):
        _logger.warning("central answered HTTP %s (%s) for a payload of %d bytes: dropped, not retried",
                        exc.status, exc, len(body))

    def flush_spool(self):
        """Push spooled payloads oldest-first (bounded). Returns number flushed; raises on a retryable
        failure (network, 5xx, 401, 404, 429). A payload refused with another 4xx is dropped."""
        n = 0
        for spool_id, body in self.spool.oldest(int(self.cfg["flush_batch"])):
            try:
                self.central.push(body)
            except PushError as exc:
                if not exc.permanent:
                    raise
                self._drop_rejected(body, exc)
            self.spool.delete(spool_id)
            n += 1
        return n

    def push(self, payload):
        body = json.dumps(payload, separators=(",", ":"), default=str)
        if self.clock() < self._push_after:
            self.spool.add(body)
            return False
        try:
            flushed = self.flush_spool()
            accepted = self.central.push(body)
        except PushError as exc:
            if exc.permanent:  # the fresh payload itself was refused: never spool it
                self._drop_rejected(body, exc)
                return False
            delay = self._backoff()
            self.spool.add(body)
            _logger.warning("push failed (%s); spooled, next attempt in %ds (%d spooled)",
                            exc, delay, self.spool.count())
            return False
        failed, self._fail_count, self._push_after = self._fail_count, 0, 0.0
        left = self.spool.count() if (failed or flushed or self._spool_pending) else 0
        if failed:
            _logger.info("push ok after %d failed attempts (flushed %d spooled, %d left)", failed, flushed, left)
        elif left:
            _logger.info("push ok (flushed %d spooled, %d left)", flushed, left)
        else:
            _logger.debug("pushed payload (%d accepted), flushed %d spooled", accepted, flushed)
        if left:
            self._spool_pending = True
        elif self._spool_pending or flushed:
            _logger.info("spool drained")
            self._spool_pending = False
        return True

    def run_once(self, dry_run=False):
        self.collect_os()
        self.collect_live()
        self.collect_topsql()
        payload = self.build_payload()
        if dry_run:
            print(json.dumps(payload, indent=2, default=str))
            return True
        return self.push(payload)

    def _due(self, name, interval, now):
        if now >= self._next.get(name, 0):
            self._next[name] = now + interval
            return True
        return False

    def run_forever(self):
        _logger.info("perf collector %s started (central=%s db=%s)", __version__,
                     self.cfg["central_url"], self.cfg.get("central_db"))
        # first sample primes cpu/net counters; skip it for rates
        self.sampler.sample()
        self._next["os"] = self.clock() + 5  # give rate counters a few seconds
        self._next["push"] = self.clock() + self.cfg["push_interval"]
        while not self.stop_event.is_set():
            now = self.clock()
            for name, fn, key in (("os", self.collect_os, "sample_interval"),
                                  ("live", self.collect_live, "live_interval"),
                                  ("sql", self.collect_topsql, "topsql_interval")):
                if self._due(name, self.cfg[key], now):
                    try:
                        fn()
                    except Exception:
                        _logger.exception("collector step %s failed", name)
            if self._due("push", self.cfg["push_interval"], now):
                try:
                    self.push(self.build_payload())
                except Exception:
                    _logger.exception("push step failed")
            self.stop_event.wait(1.0)
        try:  # best effort: keep what we have
            if self.metrics or self.sql or self.live:
                self.spool.add(self.build_payload())
        except Exception:
            pass
        _logger.info("perf collector stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Perf Monitor collector agent")
    ap.add_argument("--config", default="/etc/perf-monitor/collector.conf")
    ap.add_argument("--once", action="store_true", help="collect + push one cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="print payload JSON, do not push")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)

    cfg = load_config(args.config if os.path.exists(args.config) or not args.dry_run else None)
    logging.basicConfig(level=getattr(logging, cfg["log_level"].upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.dry_run and not cfg["token"]:
        _logger.error("no token configured")
        return 2
    try:
        collector = Collector(cfg)
    except ValueError as exc:
        _logger.error("invalid configuration: %s", exc)
        return 2

    def _stop(signum, frame):
        _logger.info("signal %s received, stopping", signum)
        collector.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.once or args.dry_run:
        collector.sampler.sample()  # prime rate counters
        time.sleep(1.0)
        ok = collector.run_once(dry_run=args.dry_run)
        return 0 if ok else 1
    collector.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
