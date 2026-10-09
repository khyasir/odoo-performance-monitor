# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """16.0.1.10.0: `perf.server.host_server_id` becomes a stored field (it was computed on the fly from
    db_name). Create the column first and fill it with the OLD link (the first active server card serving
    the same database), so no agent card loses its server on upgrade. Odoo does not recompute a stored
    field whose column already exists. post-migrate then prefers an exact name match."""
    cr.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'perf_server' AND column_name = 'host_server_id'""")
    if cr.fetchone():
        return
    cr.execute('ALTER TABLE perf_server ADD COLUMN host_server_id integer')
    cr.execute("""
        UPDATE perf_server a SET host_server_id = m.cid
          FROM (SELECT DISTINCT ON (a2.id) a2.id AS aid, c.id AS cid
                  FROM perf_server a2
                  JOIN perf_server c ON c.db_name = a2.db_name AND c.source_kind = 'collector'
                                    AND c.active AND c.id != a2.id
                 WHERE a2.source_kind = 'agent' AND a2.db_name IS NOT NULL
                 ORDER BY a2.id, c.environment DESC, c.name) m
         WHERE a.id = m.aid
    """)
    _logger.info('perf_monitor: kept the old database-name server link on %d agent card(s)', cr.rowcount)
