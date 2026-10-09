# -*- coding: utf-8 -*-
import logging
import threading
from datetime import timedelta

from odoo import fields

_logger = logging.getLogger(__name__)

# Every cron of this module MUST start with this prefix: staging hygiene scripts
# deactivate all crons except the ones whose name starts with it.
CRON_PREFIX = 'Perf Monitor: '
PURGE_BATCH = 10000
# where collector/install.sh.tmpl writes the collector config and its systemd unit (keep in sync)
COLLECTOR_CONF_PATH = '/etc/perf-monitor/collector.conf'
COLLECTOR_RESTART_COMMAND = 'sudo systemctl restart perf-monitor-collector'


def create_server_ts_index(cr, table):
    """Composite (server_id, ts DESC) index used by every per-server time series."""
    cr.execute('CREATE INDEX IF NOT EXISTS %s_server_ts_idx ON %s (server_id, ts DESC)' % (table, table))


def get_int_param(env, key, default):
    try:
        return int(env['ir.config_parameter'].sudo().get_param(key, default))
    except (TypeError, ValueError):
        return default


def get_float_param(env, key, default):
    """Settings numbers are deleted (not stored as 0) when saved empty: the default is applied here."""
    try:
        return float(env['ir.config_parameter'].sudo().get_param(key) or default)
    except (TypeError, ValueError):
        return default


def purge_older_than(model, days, batch=PURGE_BATCH):
    """Delete rows of ``model`` older than ``days`` in short batches (short locks). Rows dated more
    than one day in the future (stored before ingestion checked ``ts``, or by a skewed clock) are
    deleted too, otherwise they would never age out."""
    cr = model.env.cr
    table = model._table
    now = fields.Datetime.now()
    cutoff = now - timedelta(days=days)
    future = now + timedelta(days=1)
    model.flush_model()
    total = 0
    commit = not getattr(threading.current_thread(), 'testing', False)
    while True:
        cr.execute(
            'DELETE FROM %s WHERE id IN (SELECT id FROM %s WHERE ts < %%s OR ts > %%s LIMIT %%s)' % (table, table),
            (cutoff, future, batch))
        total += cr.rowcount
        if cr.rowcount < batch:
            break
        if commit:
            cr.commit()
    model.invalidate_model()
    _logger.info('perf_monitor: purged %s rows from %s (older than %s days or dated > 1 day ahead)',
                 total, table, days)
    return total
