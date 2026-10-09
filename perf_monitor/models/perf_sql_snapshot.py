# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .perf_common import create_server_ts_index, get_int_param, purge_older_than


class PerfSqlSnapshot(models.Model):
    _name = 'perf.sql.snapshot'
    _description = 'Top SQL Snapshot (pg_stat_statements interval delta)'
    _order = 'ts desc, total_ms desc'
    _rec_name = 'queryid'

    server_id = fields.Many2one('perf.server', required=True, ondelete='cascade', index=True)
    ts = fields.Datetime('Timestamp', required=True, index=True)
    queryid = fields.Char('Query ID', index=True)
    query = fields.Text('Query (normalized, literals redacted)')
    # counters are Float (numeric, 0 decimals): cumulative pg_stat_statements values overflow int4
    calls = fields.Float('Calls', digits=(20, 0))
    total_ms = fields.Float('Total time (ms)', digits=(16, 3))
    mean_ms = fields.Float('Mean time (ms)', digits=(16, 3), group_operator='avg')
    max_ms = fields.Float('Max time (ms)', digits=(16, 3), group_operator='max',
                          help='Cumulative maximum reported by pg_stat_statements')
    cum_mean_ms = fields.Float('Cumulative mean (ms)', digits=(16, 3), group_operator='avg')
    rows = fields.Float('Rows', digits=(20, 0))
    shared_blks_hit = fields.Float('Shared blocks hit', digits=(20, 0))
    shared_blks_read = fields.Float('Shared blocks read', digits=(20, 0))
    temp_blks_read = fields.Float('Temp blocks read', digits=(20, 0))
    temp_blks_written = fields.Float('Temp blocks written', digits=(20, 0))
    reason = fields.Char('Reason tags', help='slow_mean, high_calls, io, temp')

    def init(self):
        super().init()
        create_server_ts_index(self.env.cr, self._table)

    @api.model
    def _cron_purge(self):
        days = get_int_param(self.env, 'perf_monitor.sql_retention_days', 14)
        return purge_older_than(self, days)
