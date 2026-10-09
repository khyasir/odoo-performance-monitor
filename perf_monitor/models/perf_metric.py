# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .perf_common import create_server_ts_index, get_int_param, purge_older_than

METRIC_FIELDS = (
    'cpu_pct', 'load1', 'load5', 'load15', 'iowait_pct', 'ram_total_mb', 'ram_used_mb', 'ram_pct',
    'swap_used_mb', 'disk_total_gb', 'disk_used_gb', 'disk_pct', 'net_rx_kbps', 'net_tx_kbps',
    'db_latency_ms', 'pg_connections',
)


class PerfMetric(models.Model):
    _name = 'perf.metric'
    _description = 'Performance Metric Sample'
    _order = 'ts desc'
    _rec_name = 'ts'

    server_id = fields.Many2one('perf.server', required=True, ondelete='cascade', index=True)
    ts = fields.Datetime('Timestamp', required=True, index=True)
    cpu_pct = fields.Float('CPU %', group_operator='avg')
    load1 = fields.Float('Load 1m', group_operator='avg')
    load5 = fields.Float('Load 5m', group_operator='avg')
    load15 = fields.Float('Load 15m', group_operator='avg')
    iowait_pct = fields.Float('IO wait %', group_operator='avg')
    ram_total_mb = fields.Float('RAM total (MB)', group_operator='avg')
    ram_used_mb = fields.Float('RAM used (MB)', group_operator='avg')
    ram_pct = fields.Float('RAM %', group_operator='avg')
    swap_used_mb = fields.Float('Swap used (MB)', group_operator='avg')
    disk_total_gb = fields.Float('Disk total (GB)', group_operator='avg')
    disk_used_gb = fields.Float('Disk used (GB)', group_operator='avg')
    disk_pct = fields.Float('Disk %', group_operator='avg')
    net_rx_kbps = fields.Float('Net RX (kbit/s)', group_operator='avg')
    net_tx_kbps = fields.Float('Net TX (kbit/s)', group_operator='avg')
    db_latency_ms = fields.Float('DB latency (ms)', digits=(16, 3), group_operator='avg')
    pg_connections = fields.Integer('PG connections', group_operator='avg')

    def init(self):
        super().init()
        create_server_ts_index(self.env.cr, self._table)

    @api.model
    def _cron_purge(self):
        days = get_int_param(self.env, 'perf_monitor.metric_retention_days', 35)
        return purge_older_than(self, days)
