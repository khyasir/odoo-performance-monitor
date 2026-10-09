# -*- coding: utf-8 -*-
"""AI models table (Configuration > AI models): one row per Bedrock model / inference profile with its region,
prices (cost log) and whether Converse may receive a ``temperature`` (newer models such as Claude Opus 5.5
refuse it: "temperature is deprecated for this model"). Settings > Performance > "Default AI model" picks the
row used by new issues; every issue (and its chat) can use another row."""
from odoo import api, fields, models

from .perf_common import get_int_param

DEFAULT_MODEL_PARAM = 'perf_monitor.ai_default_model_id'    # perf.ai.model id (Settings > Default AI model)
LEGACY_MODEL_PARAM = 'perf_monitor.ai_model_id'             # free-text model id (before 16.0.1.12.0): fallback


class PerfAiModel(models.Model):
    _name = 'perf.ai.model'
    _description = 'Performance AI model (Bedrock)'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    bedrock_model_id = fields.Char('Bedrock model / inference profile id', required=True,
                                   help='e.g. eu.anthropic.claude-haiku-4-5-20251001-v1:0')
    region = fields.Char('Bedrock region', required=True, default=lambda self: self._default_region())
    price_input_mtok = fields.Float('Input USD / 1M tokens', digits=(12, 4), default=1.0)
    price_output_mtok = fields.Float('Output USD / 1M tokens', digits=(12, 4), default=5.0)
    send_temperature = fields.Boolean('Send temperature', default=True,
                                      help='Untick for models that refuse the Converse temperature parameter '
                                           '(e.g. Claude Opus 5.5: "temperature is deprecated for this model"). '
                                           'A call refused for that reason is retried once without it.')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    @api.model
    def _default_region(self):
        from .perf_ai import DEFAULT_REGION  # pylint: disable=import-outside-toplevel
        return self.env['ir.config_parameter'].sudo().get_param('perf_monitor.ai_region') or DEFAULT_REGION

    @api.model
    def _default_model(self):
        """Settings "Default AI model"; else the active row whose id is the legacy perf_monitor.ai_model_id;
        else the first active row; else an empty recordset (the legacy parameters are used then)."""
        Model = self.sudo()
        record = Model.browse(get_int_param(self.env, DEFAULT_MODEL_PARAM, 0)).exists()
        if record and record.active:
            return record
        legacy = (self.env['ir.config_parameter'].sudo().get_param(LEGACY_MODEL_PARAM) or '').strip()
        return (legacy and Model.search([('bedrock_model_id', '=', legacy)], limit=1)) or Model.search([], limit=1)

    def name_get(self):
        return [(rec.id, '%s (%s)' % (rec.name, rec.bedrock_model_id) if rec.name != rec.bedrock_model_id
                 else rec.name) for rec in self]
