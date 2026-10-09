# -*- coding: utf-8 -*-
"""Right-size card: 14-day p95 CPU / RAM / connections of the EC2 instance and the RDS instance of
a collector server, from CloudWatch (boto3, credentials of the server's IAM role), with a
downsize / keep / upsize recommendation and the monthly saving (on-demand, Linux / PostgreSQL
Single-AZ; AWS Pricing API, static eu-west-1 table when it is not allowed).

EC2 RAM is not a CloudWatch metric without the CloudWatch agent: CWAgent mem_used_percent when it
exists, else the collector's ram_pct samples (perf.metric). Each metric needs 14 distinct days;
until then the card says "insufficient data: N/14 days".
"""
import json
import logging
import math
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.tools import config

from .perf_common import get_int_param

_logger = logging.getLogger(__name__)

WINDOW_DAYS = 14
PERIOD_S = 300
HOURS_PER_MONTH = 730
UPSIZE_CPU, UPSIZE_MEM, UPSIZE_CONN = 70.0, 85.0, 80.0   # p95 above -> upsize
TARGET_CPU, TARGET_MEM, TARGET_CONN = 60.0, 75.0, 60.0   # projected p95 on the smaller size must stay below
DEFAULT_AWS_REGION = 'eu-west-1'
PRICING_LOCATION = {'eu-west-1': 'EU (Ireland)', 'eu-central-1': 'EU (Frankfurt)', 'me-south-1': 'Middle East (Bahrain)',
                    'me-central-1': 'Middle East (UAE)', 'us-east-1': 'US East (N. Virginia)'}
SIZES = ['nano', 'micro', 'small', 'medium', 'large', 'xlarge', '2xlarge', '4xlarge', '8xlarge', '12xlarge',
         '16xlarge']

# (vCPU, GiB) of common types; describe_instance_types (EC2) wins when allowed.
EC2_SPECS = {
    't2.micro': (1, 1), 't2.small': (1, 2), 't2.medium': (2, 4), 't2.large': (2, 8), 't2.xlarge': (4, 16),
    't2.2xlarge': (8, 32),
    't3.micro': (2, 1), 't3.small': (2, 2), 't3.medium': (2, 4), 't3.large': (2, 8), 't3.xlarge': (4, 16),
    't3.2xlarge': (8, 32), 't3a.medium': (2, 4), 't3a.large': (2, 8), 't3a.xlarge': (4, 16), 't3a.2xlarge': (8, 32),
    't4g.medium': (2, 4), 't4g.large': (2, 8), 't4g.xlarge': (4, 16), 't4g.2xlarge': (8, 32),
    'm5.large': (2, 8), 'm5.xlarge': (4, 16), 'm5.2xlarge': (8, 32), 'm5.4xlarge': (16, 64),
    'm6i.large': (2, 8), 'm6i.xlarge': (4, 16), 'm6i.2xlarge': (8, 32), 'm6i.4xlarge': (16, 64),
    'm7i.large': (2, 8), 'm7i.xlarge': (4, 16), 'm7i.2xlarge': (8, 32),
    'm7g.large': (2, 8), 'm7g.xlarge': (4, 16), 'm7g.2xlarge': (8, 32), 'm7g.4xlarge': (16, 64),
    'c5.large': (2, 4), 'c5.xlarge': (4, 8), 'c5.2xlarge': (8, 16), 'r5.large': (2, 16), 'r5.xlarge': (4, 32),
    'r5.2xlarge': (8, 64),
}
RDS_SPECS = {
    'db.t3.micro': (2, 1), 'db.t3.small': (2, 2), 'db.t3.medium': (2, 4), 'db.t3.large': (2, 8),
    'db.t3.xlarge': (4, 16), 'db.t3.2xlarge': (8, 32),
    'db.t4g.micro': (2, 1), 'db.t4g.small': (2, 2), 'db.t4g.medium': (2, 4), 'db.t4g.large': (2, 8),
    'db.t4g.xlarge': (4, 16), 'db.t4g.2xlarge': (8, 32),
    'db.m5.large': (2, 8), 'db.m5.xlarge': (4, 16), 'db.m5.2xlarge': (8, 32), 'db.m5.4xlarge': (16, 64),
    'db.m6g.large': (2, 8), 'db.m6g.xlarge': (4, 16), 'db.m6g.2xlarge': (8, 32),
    'db.m7g.large': (2, 8), 'db.m7g.xlarge': (4, 16), 'db.m7g.2xlarge': (8, 32), 'db.m7g.4xlarge': (16, 64),
    'db.r6g.large': (2, 16), 'db.r6g.xlarge': (4, 32), 'db.r6g.2xlarge': (8, 64),
    'db.r7g.large': (2, 16), 'db.r7g.xlarge': (4, 32), 'db.r7g.2xlarge': (8, 64),
}
# eu-west-1 on-demand USD / hour (AWS Pricing API, 2026-09-27): EC2 Linux shared, RDS PostgreSQL Single-AZ.
EC2_PRICES = {
    't2.medium': 0.05, 't2.large': 0.1008, 't2.xlarge': 0.2016, 't3.medium': 0.0456, 't3.large': 0.0912,
    't3.xlarge': 0.1824, 't3.2xlarge': 0.3648, 't3a.medium': 0.0408, 't3a.large': 0.0816, 't3a.xlarge': 0.1632,
    't4g.medium': 0.0368, 't4g.large': 0.0736, 't4g.xlarge': 0.1472, 'm5.large': 0.107, 'm5.xlarge': 0.214,
    'm5.2xlarge': 0.428, 'm6i.large': 0.107, 'm6i.xlarge': 0.214, 'm6i.2xlarge': 0.428, 'm7i.large': 0.11235,
    'm7i.xlarge': 0.2247, 'm7g.large': 0.091, 'm7g.xlarge': 0.1819, 'm7g.2xlarge': 0.3638, 'c5.large': 0.096,
    'c5.xlarge': 0.192, 'r5.large': 0.141, 'r5.xlarge': 0.282,
}
RDS_PRICES = {
    'db.t3.micro': 0.02, 'db.t3.small': 0.039, 'db.t3.medium': 0.078, 'db.t3.large': 0.156, 'db.t3.xlarge': 0.312,
    'db.t4g.micro': 0.017, 'db.t4g.small': 0.035, 'db.t4g.medium': 0.069, 'db.t4g.large': 0.138,
    'db.t4g.xlarge': 0.277, 'db.m5.large': 0.197, 'db.m5.xlarge': 0.394, 'db.m6g.large': 0.176,
    'db.m6g.xlarge': 0.352, 'db.m7g.large': 0.186, 'db.m7g.xlarge': 0.372, 'db.m7g.2xlarge': 0.745,
    'db.r6g.large': 0.252, 'db.r6g.xlarge': 0.504, 'db.r7g.large': 0.268,
}


# ---------------------------------------------------------------------------- pure helpers (tested)
def p95(values):
    """95th percentile (linear interpolation, same as percentile_cont(0.95)); None when empty."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = 0.95 * (len(values) - 1)
    low, high = int(math.floor(pos)), int(math.ceil(pos))
    return values[low] + (values[high] - values[low]) * (pos - low)


def distinct_days(stamps):
    return len({ts.date() for ts in stamps if ts})


def neighbour(itype, step):
    """Same family, one size smaller (step=-1) / bigger (+1): 't2.large' -> 't2.medium'."""
    prefix, _dot, size = itype.rpartition('.')
    if size not in SIZES:
        return None
    idx = SIZES.index(size) + step
    return '%s.%s' % (prefix, SIZES[idx]) if 0 <= idx < len(SIZES) else None


def rds_max_connections(mem_gib):
    """RDS PostgreSQL default: LEAST(DBInstanceClassMemory / 9531392, 5000)."""
    return int(min(mem_gib * 1024 ** 3 / 9531392, 5000)) if mem_gib else 0


def recommend(kind, itype, specs, prices, cpu, mem, conn=None):
    """Recommendation for one instance.

    cpu / mem / conn: dict(p95=float|None, days=int) (conn: p95 connections, only RDS).
    specs: {type: (vcpu, gib)}; prices: {type: usd_hour}.
    Returns dict(action=insufficient|keep|downsize|upsize, target, reason, saving_month, price_hour).
    """
    need = WINDOW_DAYS
    price = prices.get(itype)
    res = {'action': 'keep', 'target': itype, 'reason': '', 'saving_month': 0.0, 'price_hour': price or 0.0}
    if cpu['days'] < need or cpu['p95'] is None:
        res.update(action='insufficient', reason='insufficient data: %d/%d days (CPU)' % (cpu['days'], need))
        return res
    spec = specs.get(itype)
    mem_ok = mem['days'] >= need and mem['p95'] is not None
    max_conn = rds_max_connections(spec[1]) if (kind == 'rds' and spec) else 0
    conn_pct = (100.0 * conn['p95'] / max_conn) if (conn and conn.get('p95') is not None and max_conn) else None
    reasons_up = []
    if cpu['p95'] > UPSIZE_CPU:
        reasons_up.append('p95 CPU %.0f%% > %.0f%%' % (cpu['p95'], UPSIZE_CPU))
    if mem_ok and mem['p95'] > UPSIZE_MEM:
        reasons_up.append('p95 RAM %.0f%% > %.0f%%' % (mem['p95'], UPSIZE_MEM))
    if conn_pct is not None and conn_pct > UPSIZE_CONN:
        reasons_up.append('p95 connections %.0f%% of max > %.0f%%' % (conn_pct, UPSIZE_CONN))
    if reasons_up:
        bigger = neighbour(itype, +1)
        res.update(action='upsize', target=bigger or itype, reason=', '.join(reasons_up))
        if bigger and price and prices.get(bigger):
            res['saving_month'] = round((price - prices[bigger]) * HOURS_PER_MONTH, 2)  # negative = extra cost
        return res
    if not mem_ok:
        res['reason'] = 'RAM: insufficient data: %d/%d days - no downsize without RAM evidence' % (mem['days'], need)
        return res
    smaller = neighbour(itype, -1)
    if not smaller or smaller not in specs or not spec:
        res['reason'] = 'no smaller size known in this family'
        return res
    vcpu, gib = spec
    s_vcpu, s_gib = specs[smaller]
    proj_cpu = cpu['p95'] * vcpu / s_vcpu
    proj_mem = mem['p95'] * gib / s_gib
    proj_conn = (100.0 * conn['p95'] / rds_max_connections(s_gib)) if conn_pct is not None else None
    blockers = []
    if proj_cpu > TARGET_CPU:
        blockers.append('CPU would be %.0f%%' % proj_cpu)
    if proj_mem > TARGET_MEM:
        blockers.append('RAM would be %.0f%%' % proj_mem)
    if proj_conn is not None and proj_conn > TARGET_CONN:
        blockers.append('connections would be %.0f%% of max' % proj_conn)
    if blockers:
        res['reason'] = 'keep: on %s %s' % (smaller, ', '.join(blockers))
        return res
    saving = round((price - prices[smaller]) * HOURS_PER_MONTH, 2) if price and prices.get(smaller) else 0.0
    res.update(action='downsize', target=smaller, saving_month=saving,
               reason='on %s p95 CPU ~%.0f%%, RAM ~%.0f%%%s' % (
                   smaller, proj_cpu, proj_mem, ', connections ~%.0f%% of max' % proj_conn if proj_conn is not None else ''))
    return res


def card_line(label, itype, reco, cpu, mem, conn=None):
    """One line of the kanban card."""
    if not itype:
        return '%s: unknown instance' % label
    head = '%s %s: ' % (label, itype)
    if reco['action'] == 'insufficient':
        return head + reco['reason']
    stats = 'p95 CPU %.0f%%' % cpu['p95']
    stats += (' · RAM %.0f%%' % mem['p95']) if mem['days'] >= WINDOW_DAYS and mem['p95'] is not None \
        else ' · RAM insufficient data: %d/%d days' % (mem['days'], WINDOW_DAYS)
    if conn and conn.get('p95') is not None:
        stats += ' · conn %.0f' % conn['p95']
    if reco['action'] == 'downsize':
        return head + 'downsize → %s, save $%.2f/mo (%s)' % (reco['target'], reco['saving_month'], stats)
    if reco['action'] == 'upsize':
        return head + 'upsize → %s, +$%.2f/mo (%s)' % (reco['target'], -reco['saving_month'], stats)
    return head + 'keep (%s)' % stats


class PerfServerRightsize(models.Model):
    _inherit = 'perf.server'

    rds_instance_id = fields.Char('RDS instance', tracking=True,
                                  help='RDS DB instance identifier (auto-detected from db_host when this central '
                                       'Odoo shares the monitored DB)')
    rs_ec2_line = fields.Char('EC2 right-size', readonly=True)
    rs_rds_line = fields.Char('RDS right-size', readonly=True)
    rs_saving_month = fields.Float('Saving / month (USD)', digits=(12, 2), readonly=True)
    rs_data = fields.Text('Right-size data (JSON)', readonly=True)
    rs_html = fields.Html('Right-size details', compute='_compute_rs_html', sanitize=True)

    def _compute_rs_html(self):
        for server in self:
            server.rs_html = server._rs_render(json.loads(server.rs_data)) if server.rs_data else False

    @staticmethod
    def _rs_render(data):
        rows = []
        for part in ('ec2', 'rds'):
            item = data.get(part)
            if not item:
                continue
            reco = item.get('reco') or {}

            def cell(metric, fmt='%.1f%%'):
                m = item.get(metric) or {}
                value = (fmt % m['p95']) if m.get('p95') is not None else '-'
                return '%s <small class="text-muted">(%s/%d d, %s)</small>' % (
                    value, m.get('days', 0), WINDOW_DAYS, m.get('source', ''))
            rows.append('<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                        '<td>%s</td><td>%s</td><td>%s</td></tr>' % (
                            part.upper(), item.get('id') or '', item.get('type') or '?',
                            cell('cpu'), cell('mem'), cell('conn', '%.0f') if part == 'rds' else '-',
                            '%s → %s' % (reco.get('action', ''), reco.get('target', '')) if reco.get('action') in
                            ('downsize', 'upsize') else reco.get('action', ''),
                            '$%.2f' % reco.get('saving_month', 0.0) if reco.get('saving_month') else '-',
                            reco.get('reason', '')))
        if data.get('error'):
            rows.append('<tr><td colspan="9" class="text-danger">%s</td></tr>' % data['error'])
        return ('<table class="table table-sm o_perf_rightsize"><thead><tr><th/><th>Instance</th><th>Type</th>'
                '<th>p95 CPU</th><th>p95 RAM</th><th>p95 conn.</th><th>Recommendation</th><th>Saving/mo</th>'
                '<th>Why</th></tr></thead><tbody>%s</tbody></table><p class="text-muted small">Window %d days, '
                '%d s periods, prices %s (on-demand, USD).</p>' % (
                    ''.join(rows), WINDOW_DAYS, PERIOD_S, data.get('price_source', '')))

    # ------------------------------------------------------------------ AWS
    def _aws_region(self):
        return (self.env['ir.config_parameter'].sudo().get_param('perf_monitor.aws_region') or self.region
                or DEFAULT_AWS_REGION)

    def _aws_client(self, service, region):
        import boto3  # pylint: disable=import-outside-toplevel
        from botocore.config import Config  # pylint: disable=import-outside-toplevel
        return boto3.client(service, region_name=region, config=Config(
            connect_timeout=5, read_timeout=30, retries={'max_attempts': 2, 'mode': 'standard'}))

    def _cw_series(self, cw, namespace, metric, dims, start, end, stat='Average'):
        """[(ts, value)] of one CloudWatch metric (PERIOD_S periods, paginated)."""
        points, token = [], None
        while True:
            kwargs = dict(MetricDataQueries=[{
                'Id': 'm', 'ReturnData': True, 'MetricStat': {
                    'Metric': {'Namespace': namespace, 'MetricName': metric,
                               'Dimensions': [{'Name': k, 'Value': v} for k, v in dims.items()]},
                    'Period': PERIOD_S, 'Stat': stat}}], StartTime=start, EndTime=end)
            if token:
                kwargs['NextToken'] = token
            resp = cw.get_metric_data(**kwargs)
            for result in resp.get('MetricDataResults', []):
                points.extend(zip(result.get('Timestamps', []), result.get('Values', [])))
            token = resp.get('NextToken')
            if not token:
                return points

    @staticmethod
    def _stat(points, source, transform=None):
        values = [transform(v) if transform else v for _ts, v in points]
        return {'p95': p95(values), 'days': distinct_days([ts for ts, _v in points]), 'source': source,
                'samples': len(values)}

    def _collector_ram(self, start):
        self.ensure_one()
        self.env['perf.metric'].flush_model()
        self.env.cr.execute('SELECT ts, ram_pct FROM perf_metric WHERE server_id = %s AND ts >= %s',
                            (self.id, start))
        return self.env.cr.fetchall()

    def _price(self, pricing, kind, itype, region, cache):
        """On-demand USD/hour from the Pricing API (cached per run), static table as fallback."""
        if (kind, itype) in cache:
            return cache[(kind, itype)]
        static = (EC2_PRICES if kind == 'ec2' else RDS_PRICES).get(itype)
        price, location = None, PRICING_LOCATION.get(region)
        if pricing is not None and location:
            try:
                if kind == 'ec2':
                    filters = {'instanceType': itype, 'location': location, 'operatingSystem': 'Linux',
                               'tenancy': 'Shared', 'preInstalledSw': 'NA', 'capacitystatus': 'Used',
                               'licenseModel': 'No License required'}
                    service = 'AmazonEC2'
                else:
                    filters = {'instanceType': itype, 'location': location, 'databaseEngine': 'PostgreSQL',
                               'deploymentOption': 'Single-AZ'}
                    service = 'AmazonRDS'
                resp = pricing.get_products(ServiceCode=service, MaxResults=5, Filters=[
                    {'Type': 'TERM_MATCH', 'Field': k, 'Value': v} for k, v in filters.items()])
                for item in resp.get('PriceList', []):
                    product = json.loads(item) if isinstance(item, str) else item
                    for term in product.get('terms', {}).get('OnDemand', {}).values():
                        for dim in term.get('priceDimensions', {}).values():
                            usd = float(dim.get('pricePerUnit', {}).get('USD') or 0)
                            if usd > 0:
                                price = usd
            except Exception as exc:  # pylint: disable=broad-except
                _logger.info('perf_monitor: pricing API unavailable (%s), static table used', exc)
                cache['_static'] = True
        if price is None:
            price = static
            cache['_static'] = True
        cache[(kind, itype)] = price
        return price

    def _detect_rds(self, rds):
        """RDS instance whose endpoint is this Odoo's db_host (central == monitored DB)."""
        self.ensure_one()
        host = (config.get('db_host') or '').strip().lower()
        if not host or self.db_name != self.env.cr.dbname:
            return None
        for page in rds.get_paginator('describe_db_instances').paginate():
            for inst in page.get('DBInstances', []):
                if (inst.get('Endpoint') or {}).get('Address', '').lower() == host:
                    return inst['DBInstanceIdentifier']
        return None

    def _rightsize_from_aws(self):
        """Compute the card data for one collector server. Raises on AWS errors (caller catches)."""
        self.ensure_one()
        region = self._aws_region()
        end = datetime.utcnow().replace(second=0, microsecond=0)
        start = end - timedelta(days=WINDOW_DAYS)
        cw = self._aws_client('cloudwatch', region)
        try:
            pricing = self._aws_client('pricing', 'us-east-1')
        except Exception:  # pylint: disable=broad-except
            pricing = None
        price_cache = {}
        data = {'computed_at': fields.Datetime.to_string(fields.Datetime.now()), 'region': region}
        ec2_specs, rds_specs = dict(EC2_SPECS), dict(RDS_SPECS)
        ec2_prices, rds_prices = {}, {}
        if self.instance_id:
            ec2 = self._aws_client('ec2', region)
            desc = ec2.describe_instances(InstanceIds=[self.instance_id])
            inst = desc['Reservations'][0]['Instances'][0]
            itype = inst['InstanceType']
            cpu = self._stat(self._cw_series(cw, 'AWS/EC2', 'CPUUtilization', {'InstanceId': self.instance_id},
                                             start, end), 'CloudWatch')
            mem_points = self._cw_series(cw, 'CWAgent', 'mem_used_percent', {'InstanceId': self.instance_id},
                                         start, end)
            mem = self._stat(mem_points, 'CWAgent') if mem_points else \
                self._stat([(ts, v) for ts, v in self._collector_ram(start)], 'collector')
            family = [t for t in (neighbour(itype, -1), itype, neighbour(itype, +1)) if t]
            try:
                for spec in ec2.describe_instance_types(InstanceTypes=family)['InstanceTypes']:
                    ec2_specs[spec['InstanceType']] = (spec['VCpuInfo']['DefaultVCpus'],
                                                       spec['MemoryInfo']['SizeInMiB'] / 1024.0)
            except Exception as exc:  # pylint: disable=broad-except
                _logger.info('perf_monitor: describe_instance_types failed (%s), static specs used', exc)
            for t in family:
                ec2_prices[t] = self._price(pricing, 'ec2', t, region, price_cache)
            reco = recommend('ec2', itype, ec2_specs, ec2_prices, cpu, mem)
            data['ec2'] = {'id': self.instance_id, 'type': itype, 'cpu': cpu, 'mem': mem, 'reco': reco,
                           'line': card_line('EC2', itype, reco, cpu, mem)}
        rds = self._aws_client('rds', region)
        rds_id = self.rds_instance_id or self._detect_rds(rds)
        if rds_id:
            inst = rds.describe_db_instances(DBInstanceIdentifier=rds_id)['DBInstances'][0]
            itype = inst['DBInstanceClass']
            gib = (rds_specs.get(itype) or (0, 0))[1]
            dims = {'DBInstanceIdentifier': rds_id}
            cpu = self._stat(self._cw_series(cw, 'AWS/RDS', 'CPUUtilization', dims, start, end), 'CloudWatch')
            mem = self._stat(self._cw_series(cw, 'AWS/RDS', 'FreeableMemory', dims, start, end, stat='Minimum'),
                             'CloudWatch', transform=(lambda free: max(0.0, 100.0 - 100.0 * free / (gib * 1024 ** 3)))
                             if gib else None)
            if not gib:
                mem.update(p95=None, days=0)
            conn = self._stat(self._cw_series(cw, 'AWS/RDS', 'DatabaseConnections', dims, start, end,
                                              stat='Maximum'), 'CloudWatch')
            for t in (neighbour(itype, -1), itype, neighbour(itype, +1)):
                if t:
                    rds_prices[t] = self._price(pricing, 'rds', t, region, price_cache)
            reco = recommend('rds', itype, rds_specs, rds_prices, cpu, mem, conn)
            data['rds'] = {'id': rds_id, 'type': itype, 'multi_az': inst.get('MultiAZ'), 'cpu': cpu, 'mem': mem,
                           'conn': conn, 'max_connections': rds_max_connections(gib), 'reco': reco,
                           'line': card_line('RDS', itype, reco, cpu, mem, conn)}
            if inst.get('MultiAZ'):
                data['rds']['line'] += ' (Multi-AZ: prices x2)'
        data['price_source'] = 'static eu-west-1 table' if price_cache.get('_static') else 'AWS Pricing API'
        return data, rds_id

    def _compute_rightsize_aws(self):
        """Refresh the card of each collector server. Fail-safe: an AWS error is shown on the card
        and the collector-only text is kept."""
        now = fields.Datetime.now()
        for server in self.filtered(lambda s: s.source_kind != 'agent'):
            vals = {'rightsize_date': now}
            try:
                data, rds_id = server._rightsize_from_aws()
                if rds_id and not server.rds_instance_id:
                    vals['rds_instance_id'] = rds_id
            except Exception as exc:  # pylint: disable=broad-except
                _logger.warning('perf_monitor: right-size from CloudWatch failed for server %s: %s', server.id, exc)
                data = dict(json.loads(server.rs_data or '{}'), error='CloudWatch unavailable: %s' % str(exc)[:300])
            saving = sum(max(0.0, (data.get(p) or {}).get('reco', {}).get('saving_month', 0.0) or 0.0)
                         for p in ('ec2', 'rds')
                         if (data.get(p) or {}).get('reco', {}).get('action') == 'downsize')
            lines = [data[p]['line'] for p in ('ec2', 'rds') if data.get(p)]
            vals.update({
                'rs_data': json.dumps(data, default=str),
                'rs_ec2_line': (data.get('ec2') or {}).get('line') or False,
                'rs_rds_line': (data.get('rds') or {}).get('line') or data.get('error') or False,
                'rs_saving_month': saving,
            })
            if lines:
                vals['rightsize_recommendation'] = '\n'.join(lines)
            server.write(vals)
        return True

    def action_compute_rightsize(self):
        super().action_compute_rightsize()  # collector-only text first (kept when AWS fails)
        self._compute_rightsize_aws()
        return True

    @api.model
    def _cron_rightsize(self):
        """Cron "Perf Monitor: Right-size from CloudWatch" (daily)."""
        hours = get_int_param(self.env, 'perf_monitor.rightsize_min_hours', 20)
        cutoff = fields.Datetime.now() - timedelta(hours=hours)
        servers = self.search([('source_kind', '!=', 'agent')]).filtered(
            lambda s: not s.rs_data or not s.rightsize_date or s.rightsize_date < cutoff)
        servers.action_compute_rightsize()
        return True

    @api.model
    def _cron_refresh(self):
        """Status every 5 min; the collector-only right-size text only (CloudWatch is daily)."""
        servers = self.search([])
        servers._refresh_status()
        collectors = servers.filtered(lambda s: s.source_kind != 'agent')
        for server_id, text in collectors._compute_rightsize_recommendation().items():
            server = self.browse(server_id)
            data = json.loads(server.rs_data or '{}')
            if not (data.get('ec2') or data.get('rds')) and server.rightsize_recommendation != text:
                server.write({'rightsize_recommendation': text, 'rightsize_date': fields.Datetime.now()})
        return True
