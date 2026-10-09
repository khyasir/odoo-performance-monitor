#!/usr/bin/env python3
"""Overhead benchmark of perf_monitor_agent: N sequential HTTP requests per workload against a DB
WITH the agent and a DB WITHOUT it (or the same DB before / after install), rounds interleaved
(A B A B ...) to cancel drift. Stdlib only; prints a table and writes JSON (--json).

  bench_overhead.py --url http://127.0.0.1:8069 --target with=db_agent --target without=db_plain \\
      --login admin --password admin --n 300 --rounds 4
  bench_overhead.py --url http://127.0.0.1:8069 --target before=demo_db --public-only --n 300
  bench_overhead.py --micro            # in-process cost of the per-statement query hook

Workloads: version_info (JSON, auth none, ~0 SQL: worst case in %), login_page (GET /web/login,
public, template + ~20 SQL), and with credentials search_read (res.partner, 80 rows) and
read_one (res.partner, 1 row). The password is read from --password or $BENCH_PASSWORD.
"""
import argparse
import http.cookiejar
import json
import os
import statistics
import sys
import time
import urllib.request

WORKLOADS_PUBLIC = ('version_info', 'login_page')
WORKLOADS_AUTH = ('search_read', 'read_one')


class Client(object):
    def __init__(self, url, db):
        self.url, self.db = url.rstrip('/'), db
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.partner_id = None

    def _open(self, path, body=None):
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        req = urllib.request.Request(self.url + path, data=body, headers=headers)
        with self.opener.open(req, timeout=60) as resp:
            data = resp.read()
            if resp.status != 200:
                raise RuntimeError('%s -> HTTP %s' % (path, resp.status))
            return data

    def rpc(self, path, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': params}).encode()
        data = json.loads(self._open(path, body))
        if 'error' in data:
            raise RuntimeError('%s: %s' % (path, data['error'].get('data', {}).get('message') or data['error']))
        return data.get('result')

    def pin(self):
        try:
            self._open('/web?db=%s' % self.db)
        except Exception:
            pass  # redirect to the login page still pins the DB in the session cookie

    def login(self, login, password):
        self.rpc('/web/session/authenticate', {'db': self.db, 'login': login, 'password': password})
        ids = self.rpc('/web/dataset/call_kw/res.partner/search', {
            'model': 'res.partner', 'method': 'search', 'args': [[]], 'kwargs': {'limit': 1}})
        self.partner_id = ids[0]

    def run(self, workload):
        if workload == 'version_info':
            return self.rpc('/web/webclient/version_info', {})
        if workload == 'login_page':
            return self._open('/web/login')
        if workload == 'search_read':
            return self.rpc('/web/dataset/call_kw/res.partner/search_read', {
                'model': 'res.partner', 'method': 'search_read', 'args': [[]],
                'kwargs': {'fields': ['name', 'email', 'phone', 'company_id', 'country_id'], 'limit': 80}})
        if workload == 'read_one':
            return self.rpc('/web/dataset/call_kw/res.partner/read', {
                'model': 'res.partner', 'method': 'read', 'args': [[self.partner_id], ['name']], 'kwargs': {}})
        raise ValueError(workload)


def timed(client, workload, n):
    out = []
    for _i in range(n):
        t0 = time.perf_counter()
        client.run(workload)
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def summary(samples):
    samples = sorted(samples)
    return {'n': len(samples), 'median_ms': statistics.median(samples), 'mean_ms': statistics.fmean(samples),
            'p95_ms': samples[int(len(samples) * 0.95) - 1], 'trimmed_mean_ms': statistics.fmean(
                samples[len(samples) // 20: len(samples) - len(samples) // 20] or samples)}


def micro(n=200000):
    """ns per statement of CallProbe.__call__ (steady state: a few templates, as in a real request)."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), 'tools'))
    import probe as P  # noqa: E402 (pure python, no Odoo)
    templates = ['SELECT * FROM t%d WHERE id IN %%s' % i for i in range(30)]
    p = P.CallProbe('http', 0, {}, 'x')
    t0 = time.perf_counter()
    for i in range(n):
        p(None, templates[i % 30], None, 0.0, 0.0001)
    hook_ns = (time.perf_counter() - t0) / n * 1e9
    t0 = time.perf_counter()
    for i in range(n):
        P.build_app_name('demo_db', 2, 'admin', 'sale.order.web_search_read', '/web/dataset/call_kw')
    tag_ns = (time.perf_counter() - t0) / n * 1e9
    return {'hook_ns_per_statement': round(hook_ns, 1), 'build_app_name_ns': round(tag_ns, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='http://127.0.0.1:8069')
    ap.add_argument('--target', action='append', default=[], help='label=db (1 or 2 targets)')
    ap.add_argument('--login')
    ap.add_argument('--password', default=os.environ.get('BENCH_PASSWORD'))
    ap.add_argument('--public-only', action='store_true')
    ap.add_argument('--n', type=int, default=300, help='requests per workload per round')
    ap.add_argument('--rounds', type=int, default=4)
    ap.add_argument('--warmup', type=int, default=30)
    ap.add_argument('--workloads', help='comma separated subset')
    ap.add_argument('--json', help='write raw results here')
    ap.add_argument('--micro', action='store_true')
    args = ap.parse_args()
    if args.micro:
        print(json.dumps(micro(), indent=2))
        return
    targets = [t.split('=', 1) for t in args.target]
    workloads = list(WORKLOADS_PUBLIC) + ([] if args.public_only else list(WORKLOADS_AUTH))
    if args.workloads:
        workloads = [w for w in args.workloads.split(',') if w]
    clients = {}
    for label, db in targets:
        client = Client(args.url, db)
        client.pin()
        if any(w in WORKLOADS_AUTH for w in workloads):
            client.login(args.login, args.password)
        clients[label] = client
    samples = {(label, w): [] for label, _db in targets for w in workloads}
    for w in workloads:
        for label, client in clients.items():
            timed(client, w, args.warmup)
    for rnd in range(args.rounds):
        order = list(clients.items()) if rnd % 2 == 0 else list(reversed(list(clients.items())))
        for w in workloads:
            for label, client in order:
                samples[(label, w)] += timed(client, w, args.n)
        print('round %d/%d done' % (rnd + 1, args.rounds), file=sys.stderr)
    result = {'url': args.url, 'targets': dict(targets), 'n_per_round': args.n, 'rounds': args.rounds,
              'workloads': {}}
    labels = [label for label, _db in targets]
    print('%-13s %-9s %8s %8s %8s %8s' % ('workload', 'target', 'median', 'trim.mean', 'mean', 'p95'))
    for w in workloads:
        result['workloads'][w] = {}
        for label in labels:
            s = summary(samples[(label, w)])
            result['workloads'][w][label] = s
            print('%-13s %-9s %8.2f %8.2f %8.2f %8.2f' % (w, label, s['median_ms'], s['trimmed_mean_ms'],
                                                         s['mean_ms'], s['p95_ms']))
        if len(labels) == 2:
            a, b = (result['workloads'][w][label] for label in labels)
            d_med = a['median_ms'] - b['median_ms']
            d_trim = a['trimmed_mean_ms'] - b['trimmed_mean_ms']
            result['workloads'][w]['delta'] = {
                'median_ms': d_med, 'median_pct': 100.0 * d_med / b['median_ms'],
                'trimmed_mean_ms': d_trim, 'trimmed_mean_pct': 100.0 * d_trim / b['trimmed_mean_ms']}
            print('%-13s %-9s %+8.2f ms (%+.1f%%) median, %+.2f ms (%+.1f%%) trimmed mean' % (
                w, '%s-%s' % tuple(labels), d_med, 100.0 * d_med / b['median_ms'], d_trim,
                100.0 * d_trim / b['trimmed_mean_ms']))
    if args.json:
        with open(args.json, 'w') as fh:
            json.dump(result, fh, indent=2)


if __name__ == '__main__':
    main()
