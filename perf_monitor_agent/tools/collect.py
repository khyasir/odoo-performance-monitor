# -*- coding: utf-8 -*-
"""Host sampler and Top SQL delta for the in-agent collector ("Perf Monitor: agent collect").

Pure stdlib (psutil optional), no Odoo import. Minimal port of ``OsSampler`` / ``compute_top_sql`` of
``perf_monitor/collector/perf_collector.py`` (the agent must not import the central module). The cron runs
every 5 minutes, possibly in another worker process each time: the cumulative CPU / network counters of
the previous run are passed in and returned as a JSON-able state (stored in the database), so rates are
averages over the cron interval. Without a usable previous state (first run, reboot, cron stopped for a
long time) a short second sample is taken instead.
"""
import logging
import os
import socket
import time
import urllib.request

from .perf_redact import redact_sql

try:  # optional
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on host
    psutil = None

_logger = logging.getLogger(__name__)

MAX_RATE_GAP_S = 20 * 60  # older previous counters: take a short sample instead of a long average
SHORT_SAMPLE_S = 1.0
DEFAULT_THRESHOLDS = {
    'long_running_s': 5, 'idle_in_tx_s': 60, 'slow_mean_ms': 500, 'high_calls': 1000,
    'high_blks_read': 10000, 'top_n': 20, 'live_max_rows': 200, 'query_max_chars': 4000,
    'live_query_max_chars': 2000,
}
SQL_COUNTERS = ('calls', 'total_ms', 'rows', 'shared_blks_hit', 'shared_blks_read', 'temp_blks_read',
                'temp_blks_written')


# ---------------------------------------------------------------------------
# /proc parsers (same as the root collector)
# ---------------------------------------------------------------------------
def parse_proc_stat(text):
    """Return (total, idle, iowait) jiffies of the aggregate cpu line."""
    for line in text.splitlines():
        if line.startswith('cpu '):
            vals = [int(v) for v in line.split()[1:]]
            vals += [0] * (8 - len(vals))
            # user nice system idle iowait irq softirq steal (guest* already in user)
            return sum(vals[:8]), vals[3], vals[4]
    raise ValueError('no cpu line in /proc/stat')


def parse_meminfo(text):
    """Return dict of meminfo values in kB."""
    out = {}
    for line in text.splitlines():
        parts = line.split(':')
        if len(parts) != 2:
            continue
        cols = parts[1].split()
        if cols:
            try:
                out[parts[0].strip()] = int(cols[0])
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
        if ':' not in line:
            continue
        iface, data = line.split(':', 1)
        cols = data.split()
        if iface.strip() == 'lo' or len(cols) < 9:
            continue
        rx += int(cols[0])
        tx += int(cols[8])
    return rx, tx


def _read(path):
    with open(path) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Host sampler
# ---------------------------------------------------------------------------
class HostSampler:
    """CPU / RAM / swap / disk / load / network of this host. ``sample(state)`` returns (row, new_state)."""

    def __init__(self, disk_path='/', proc_root='/proc', use_psutil=True, clock=time.time, sleep=time.sleep):
        self.disk_path = disk_path
        self.proc_root = proc_root
        self.psutil = psutil if use_psutil else None
        self.clock = clock
        self.sleep = sleep

    def _proc(self, name):
        return _read(os.path.join(self.proc_root, name))

    def counters(self):
        """Cumulative counters: {'t': wall time, 'cpu': [total, idle, iowait], 'net': [rx, tx]}."""
        out = {'t': self.clock()}
        try:
            if self.psutil:
                times = self.psutil.cpu_times()
                data = times._asdict()
                total = sum(v for k, v in data.items() if k not in ('guest', 'guest_nice'))
                out['cpu'] = [total, data.get('idle', 0.0), data.get('iowait', 0.0)]
            else:
                out['cpu'] = list(parse_proc_stat(self._proc('stat')))
        except Exception as exc:  # never crash on a missing counter
            _logger.debug('cpu counters failed: %s', exc)
        try:
            if self.psutil:
                nics = self.psutil.net_io_counters(pernic=True)
                out['net'] = [sum(c.bytes_recv for n, c in nics.items() if n != 'lo'),
                              sum(c.bytes_sent for n, c in nics.items() if n != 'lo')]
            else:
                out['net'] = list(parse_net_dev(self._proc('net/dev')))
        except Exception as exc:
            _logger.debug('net counters failed: %s', exc)
        return out

    @staticmethod
    def rates(prev, cur):
        """{cpu_pct, iowait_pct, net_rx_kbps, net_tx_kbps} between two counters() (missing when unusable)."""
        row = {}
        if prev.get('cpu') and cur.get('cpu'):
            dt = float(cur['cpu'][0] - prev['cpu'][0])
            d_idle = cur['cpu'][1] - prev['cpu'][1]
            d_iowait = cur['cpu'][2] - prev['cpu'][2]
            if dt > 0 and d_idle >= 0 and d_iowait >= 0:
                row['cpu_pct'] = round(max(0.0, 100.0 * (dt - d_idle - d_iowait) / dt), 2)
                row['iowait_pct'] = round(100.0 * d_iowait / dt, 2)
        if prev.get('net') and cur.get('net'):
            dt = cur['t'] - prev['t']
            d_rx, d_tx = cur['net'][0] - prev['net'][0], cur['net'][1] - prev['net'][1]
            if dt > 0 and d_rx >= 0 and d_tx >= 0:
                row['net_rx_kbps'] = round(d_rx * 8 / 1000.0 / dt, 2)
                row['net_tx_kbps'] = round(d_tx * 8 / 1000.0 / dt, 2)
        return row

    def _usable(self, prev, cur):
        if not isinstance(prev, dict) or not prev.get('t'):
            return False
        age = cur['t'] - prev['t']
        return 0 < age <= MAX_RATE_GAP_S and bool(self.rates(prev, cur))

    def _mem(self):
        if self.psutil:
            vm, sw = self.psutil.virtual_memory(), self.psutil.swap_memory()
            return vm.total / 1048576.0, (vm.total - vm.available) / 1048576.0, sw.used / 1048576.0
        mi = parse_meminfo(self._proc('meminfo'))
        total = mi.get('MemTotal', 0) / 1024.0
        avail = mi.get('MemAvailable', mi.get('MemFree', 0) + mi.get('Cached', 0) + mi.get('Buffers', 0)) / 1024.0
        return total, total - avail, (mi.get('SwapTotal', 0) - mi.get('SwapFree', 0)) / 1024.0

    def _load(self):
        if hasattr(os, 'getloadavg'):
            try:
                return os.getloadavg()
            except OSError:
                pass
        return parse_loadavg(self._proc('loadavg'))

    def _disk(self):
        st = os.statvfs(self.disk_path)
        total = st.f_blocks * st.f_frsize / 1073741824.0
        free = st.f_bavail * st.f_frsize / 1073741824.0
        used = (st.f_blocks - st.f_bfree) * st.f_frsize / 1073741824.0
        return total, used, (100.0 * used / (used + free) if (used + free) else 0.0)  # df semantics

    def sample(self, state=None):
        """(metric row without ts, new state). CPU / net rates since the previous state when usable, else
        over a SHORT_SAMPLE_S second sample."""
        cur = self.counters()
        if not self._usable(state, cur):
            first = cur
            self.sleep(SHORT_SAMPLE_S)
            cur = self.counters()
            row = self.rates(first, cur)
        else:
            row = self.rates(state, cur)
        for name, fn in (('mem', self._mem), ('load', self._load), ('disk', self._disk)):
            try:
                res = fn()
            except Exception as exc:  # never crash on a missing counter
                _logger.debug('host sample %s failed: %s', name, exc)
                continue
            if name == 'mem':
                total, used, swap = res
                row.update(ram_total_mb=round(total, 1), ram_used_mb=round(used, 1),
                           ram_pct=round(100.0 * used / total, 2) if total else None, swap_used_mb=round(swap, 1))
            elif name == 'load':
                row['load1'], row['load5'], row['load15'] = [round(x, 2) for x in res]
            else:
                row['disk_total_gb'], row['disk_used_gb'], row['disk_pct'] = [round(x, 2) for x in res]
        return row, cur


def host_static_info():
    info = {'hostname': socket.gethostname(), 'vcpu': os.cpu_count()}
    try:
        if psutil:
            info['ram_mb'] = int(psutil.virtual_memory().total / 1048576)
        else:
            info['ram_mb'] = int(parse_meminfo(_read('/proc/meminfo')).get('MemTotal', 0) / 1024)
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# EC2 IMDSv2 (instance type for the central right-size card), cached per process
# ---------------------------------------------------------------------------
IMDS_BASE = 'http://169.254.169.254/latest'
IMDS_KEYS = {
    'instance_type': 'meta-data/instance-type',
    'instance_id': 'meta-data/instance-id',
    'az': 'meta-data/placement/availability-zone',
    'region': 'meta-data/placement/region',
}
_IMDS_CACHE = {}


def fetch_imds(base=IMDS_BASE, timeout=0.5):
    """EC2 instance metadata (IMDSv2); {} outside EC2. Queried once per process."""
    if base in _IMDS_CACHE:
        return dict(_IMDS_CACHE[base])
    out = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(base + '/api/token', method='PUT',
                                     headers={'X-aws-ec2-metadata-token-ttl-seconds': '21600'})
        token = opener.open(req, timeout=timeout).read().decode()
        for key, path in IMDS_KEYS.items():
            try:
                req = urllib.request.Request('%s/%s' % (base, path), headers={'X-aws-ec2-metadata-token': token})
                value = opener.open(req, timeout=timeout).read().decode().strip()
                if value:
                    out[key] = value
            except Exception as exc:
                _logger.debug('IMDS %s failed: %s', path, exc)
    except Exception as exc:
        _logger.debug('EC2 IMDS unavailable (%s)', exc)
    _IMDS_CACHE[base] = out
    return dict(out)


# ---------------------------------------------------------------------------
# Top SQL delta (same rules as the root collector's compute_top_sql)
# ---------------------------------------------------------------------------
def compute_top_sql(prev, current, th, ts):
    """Interval deltas of cumulative pg_stat_statements counters vs ``prev`` and the top statements.

    prev / current: {"queryid:userid": row}. Returns (selected, new_state). A negative delta (stats reset /
    entry evicted) means the current value is the delta. Selected rows have "query" redacted ("" when the
    row came without text: the caller attaches the texts of the selection).
    """
    new_state, candidates = {}, []
    for key, cur in current.items():
        new_state[key] = [float(cur.get(c) or 0) for c in SQL_COUNTERS]
        old = prev.get(key)
        if not isinstance(old, list) or len(old) != len(SQL_COUNTERS):
            old = None
        vals = new_state[key]
        reset = old is None or vals[0] < old[0] or vals[1] < old[1]
        delta = dict(zip(SQL_COUNTERS, vals if reset else [v - o for v, o in zip(vals, old)]))
        if delta['calls'] <= 0:
            continue
        mean = delta['total_ms'] / delta['calls']
        reasons = []
        if mean > th['slow_mean_ms']:
            reasons.append('slow_mean')
        if delta['calls'] >= th['high_calls']:
            reasons.append('high_calls')
        if delta['shared_blks_read'] >= th['high_blks_read']:
            reasons.append('io')
        if delta['temp_blks_read'] + delta['temp_blks_written'] > 0:
            reasons.append('temp')
        if not reasons:
            continue
        cum_calls = vals[0]
        candidates.append({
            'ts': ts, 'queryid': str(cur.get('queryid')),
            'query': (redact_sql(cur.get('query') or '') or '')[:int(th['query_max_chars'])],
            'calls': int(delta['calls']), 'total_ms': round(delta['total_ms'], 3), 'mean_ms': round(mean, 3),
            'max_ms': round(float(cur.get('max_ms') or 0), 3), 'rows': int(delta['rows']),
            'shared_blks_hit': int(delta['shared_blks_hit']), 'shared_blks_read': int(delta['shared_blks_read']),
            'temp_blks_read': int(delta['temp_blks_read']), 'temp_blks_written': int(delta['temp_blks_written']),
            'cum_mean_ms': round(vals[1] / cum_calls, 3) if cum_calls else 0.0,
            'reason': ','.join(reasons),
        })
    candidates.sort(key=lambda r: r['total_ms'], reverse=True)
    return candidates[:int(th['top_n'])], new_state


def live_category(row):
    if row.get('wait_event_type') == 'Lock':
        return 'lock_wait'
    if (row.get('state') or '').startswith('idle in transaction'):
        return 'idle_in_tx'
    return 'long_running'
