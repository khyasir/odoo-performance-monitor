# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .perf_common import create_server_ts_index, get_int_param, purge_older_than


class PerfLiveQuery(models.Model):
    _name = 'perf.live.query'
    _description = 'Live Query Sample (pg_stat_activity)'
    _order = 'ts desc, duration_s desc'
    _rec_name = 'pid'

    server_id = fields.Many2one('perf.server', required=True, ondelete='cascade', index=True)
    ts = fields.Datetime('Sampled at', required=True, index=True)
    category = fields.Selection([
        ('long_running', 'Long running'),
        ('lock_wait', 'Lock wait'),
        ('idle_in_tx', 'Idle in transaction'),
    ], required=True, default='long_running', index=True)
    pid = fields.Integer('PID')
    usename = fields.Char('DB user')
    application_name = fields.Char('Application')
    client_addr = fields.Char('Client address')
    state = fields.Char('State')
    wait_event_type = fields.Char('Wait event type')
    wait_event = fields.Char('Wait event')
    xact_start = fields.Datetime('Transaction start')
    query_start = fields.Datetime('Query start')
    duration_s = fields.Float('Duration (s)', digits=(16, 1), group_operator='max')
    blocking_pids = fields.Char('Blocked by PIDs')
    query = fields.Text('Query (literals redacted)')

    def init(self):
        super().init()
        create_server_ts_index(self.env.cr, self._table)

    @api.model
    def _cron_purge(self):
        days = get_int_param(self.env, 'perf_monitor.live_retention_days', 3)
        return purge_older_than(self, days)
