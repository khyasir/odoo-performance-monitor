# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)

PARAM_KEY = 'perf_monitor.ai_max_input_tokens'
PARAM_XMLID = 'param_ai_max_input_tokens'
OLD_DEFAULT, NEW_DEFAULT = '60000', '150000'


def migrate(cr, version):
    """16.0.1.12.0, before the data files: data/ir_config_parameter.xml now has a record for
    perf_monitor.ai_max_input_tokens (default 150000). A parameter saved earlier by the Settings has the same
    (unique) key without an XML id: link it to the new XML id (noupdate) so that loading the file does not try to
    create a second one; its value is kept, except the old default 60000 which becomes the new default 150000.
    Idempotent."""
    cr.execute('SELECT id, value FROM ir_config_parameter WHERE key = %s', (PARAM_KEY,))
    row = cr.fetchone()
    if not row:
        return
    param_id, value = row
    if (value or '').strip() == OLD_DEFAULT:
        cr.execute('UPDATE ir_config_parameter SET value = %s WHERE id = %s', (NEW_DEFAULT, param_id))
        _logger.info('perf_monitor: %s raised from the old default %s to %s', PARAM_KEY, OLD_DEFAULT, NEW_DEFAULT)
    cr.execute("SELECT 1 FROM ir_model_data WHERE module = 'perf_monitor' AND name = %s", (PARAM_XMLID,))
    if not cr.fetchone():
        cr.execute("""
            INSERT INTO ir_model_data (module, name, model, res_id, noupdate, create_date, write_date)
            VALUES ('perf_monitor', %s, 'ir.config_parameter', %s, TRUE, now() at time zone 'UTC',
                    now() at time zone 'UTC')
        """, (PARAM_XMLID, param_id))
