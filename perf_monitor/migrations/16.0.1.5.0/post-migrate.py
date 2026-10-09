# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """source_kind (new in 16.0.1.5.0): the perf.server records registered by deploy_perf_monitor_agent.sh
    (host 'agent:<db>') are DB agent sources (slow calls only); their status now follows last_push."""
    cr.execute("""
        UPDATE perf_server SET source_kind = 'agent'
         WHERE host LIKE 'agent:%%' AND source_kind IS DISTINCT FROM 'agent'
    """)
    _logger.info('perf_monitor: %s perf.server record(s) marked as DB agent sources', cr.rowcount)
    cr.execute("UPDATE perf_server SET source_kind = 'collector' WHERE source_kind IS NULL")
    # never pushed since the upgrade: no green/yellow/red left over from the collector status logic
    cr.execute("UPDATE perf_server SET status = 'unknown' WHERE source_kind = 'agent' AND last_push IS NULL")
