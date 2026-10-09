# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .perf_ai import AI_PARAM_DEFAULTS, DEFAULT_DAILY_LIMIT, DEFAULT_MODEL_ID, DEFAULT_PRICE_INPUT_MTOK, \
    DEFAULT_PRICE_OUTPUT_MTOK, DEFAULT_REGION, DEFAULT_REPO_HINT
from .perf_ai_model import DEFAULT_MODEL_PARAM
from .perf_issue import DEFAULT_MIN_MS, DEFAULT_MIN_OCC_7D, DEFAULT_MIN_QUERY_MS, DEFAULT_MIN_REPEATS, \
    DEFAULT_MIN_SQL_SHARE

# Boolean settings that default to True: stored as 'True' / 'False' (a config_parameter Boolean deletes the
# parameter when unticked, and a missing parameter must mean "default = on")
ON_BY_DEFAULT = {'perf_ai_tool_use_enabled': 'ai_tool_use_enabled', 'perf_ai_chat_enabled': 'ai_chat_enabled',
                 'perf_ai_chat_tools_enabled': 'ai_chat_tools_enabled'}
# numeric AI settings that must not be negative (0 / empty = default, see perf.issue._ai_limits)
NON_NEGATIVE = ('perf_ai_daily_limit', 'perf_ai_price_input_mtok', 'perf_ai_price_output_mtok',
                'perf_ai_max_tool_rounds',
                'perf_ai_max_input_tokens', 'perf_ai_max_output_tokens', 'perf_ai_max_files_read',
                'perf_ai_max_bytes_per_read', 'perf_ai_max_lines_per_read', 'perf_ai_snippet_max_chars',
                'perf_ai_daily_cost_limit', 'perf_ai_monthly_cost_limit', 'perf_ai_chat_max_turns',
                'perf_ai_chat_max_tokens')
MANAGER = 'perf_monitor.group_perf_manager'

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # off by default: saving the box unticked deletes the parameter (Odoo), so missing = disabled
    perf_ai_enabled = fields.Boolean('AI analysis (Bedrock)', config_parameter='perf_monitor.ai_enabled',
                                     default=False)
    perf_ai_region = fields.Char('Bedrock region', config_parameter='perf_monitor.ai_region', default=DEFAULT_REGION,
                                 help='Default region of new AI models (each AI model has its own region)')
    # the AI models table (Configuration > AI models) replaces the free-text model id and prices below, which
    # stay as the fallback when the table has no active row (perf.issue._ai_conf)
    perf_ai_default_model_id = fields.Many2one('perf.ai.model', 'Default AI model', groups=MANAGER,
                                               config_parameter=DEFAULT_MODEL_PARAM,
                                               help='Model of new issues (each issue can pick another one); prices '
                                                    'and temperature support come from its row')
    perf_ai_model_id = fields.Char('Bedrock model / inference profile (legacy fallback)',
                                   config_parameter='perf_monitor.ai_model_id', default=DEFAULT_MODEL_ID)
    perf_ai_daily_limit = fields.Integer('AI calls per day', config_parameter='perf_monitor.ai_daily_limit',
                                         default=DEFAULT_DAILY_LIMIT)
    perf_ai_price_input_mtok = fields.Float('Input USD / 1M tokens',
                                            config_parameter='perf_monitor.ai_price_input_mtok',
                                            default=DEFAULT_PRICE_INPUT_MTOK)
    perf_ai_price_output_mtok = fields.Float('Output USD / 1M tokens',
                                             config_parameter='perf_monitor.ai_price_output_mtok',
                                             default=DEFAULT_PRICE_OUTPUT_MTOK)
    # limits (saving 0 / empty deletes the parameter: perf.issue._ai_limits applies the default on read)
    perf_ai_tool_use_enabled = fields.Boolean('Let the AI read the code (read-only tools)', groups=MANAGER,
                                              help='Issue analysis (new issues, Re-analyse)')
    perf_ai_max_tool_rounds = fields.Integer('Max model calls per run', groups=MANAGER,
                                             config_parameter='perf_monitor.ai_max_tool_rounds',
                                             default=AI_PARAM_DEFAULTS['ai_max_tool_rounds'])
    perf_ai_max_input_tokens = fields.Integer('Max input tokens per run', groups=MANAGER,
                                              config_parameter='perf_monitor.ai_max_input_tokens',
                                              default=AI_PARAM_DEFAULTS['ai_max_input_tokens'])
    perf_ai_max_output_tokens = fields.Integer('Max output tokens per model call', groups=MANAGER,
                                               config_parameter='perf_monitor.ai_max_output_tokens',
                                               default=AI_PARAM_DEFAULTS['ai_max_output_tokens'])
    perf_ai_max_files_read = fields.Integer('Max files read per run', groups=MANAGER,
                                            config_parameter='perf_monitor.ai_max_files_read',
                                            default=AI_PARAM_DEFAULTS['ai_max_files_read'])
    perf_ai_max_bytes_per_read = fields.Integer('Max bytes per file read', groups=MANAGER,
                                                config_parameter='perf_monitor.ai_max_bytes_per_read',
                                                default=AI_PARAM_DEFAULTS['ai_max_bytes_per_read'])
    perf_ai_max_lines_per_read = fields.Integer('Max lines per file read', groups=MANAGER,
                                                config_parameter='perf_monitor.ai_max_lines_per_read',
                                                default=AI_PARAM_DEFAULTS['ai_max_lines_per_read'])
    perf_ai_snippet_max_chars = fields.Integer('Max characters of code context', groups=MANAGER,
                                               config_parameter='perf_monitor.ai_snippet_max_chars',
                                               default=AI_PARAM_DEFAULTS['ai_snippet_max_chars'])
    perf_ai_daily_cost_limit = fields.Float('Max USD per day', groups=MANAGER,
                                            config_parameter='perf_monitor.ai_daily_cost_limit',
                                            default=AI_PARAM_DEFAULTS['ai_daily_cost_limit'])
    perf_ai_monthly_cost_limit = fields.Float('Max USD per month', groups=MANAGER,
                                              config_parameter='perf_monitor.ai_monthly_cost_limit',
                                              default=AI_PARAM_DEFAULTS['ai_monthly_cost_limit'])
    perf_ai_chat_enabled = fields.Boolean('AI chat on issues', groups=MANAGER)
    perf_ai_chat_tools_enabled = fields.Boolean('Chat tools on/off', groups=MANAGER,
                                                help='Let the chat read the code with the read-only tools. Off: the '
                                                     'chat answers from the issue context only. Separate from the '
                                                     'issue-analysis tools switch.')
    perf_ai_chat_max_turns = fields.Integer('Chat: max answers per issue', groups=MANAGER,
                                            config_parameter='perf_monitor.ai_chat_max_turns',
                                            default=AI_PARAM_DEFAULTS['ai_chat_max_turns'])
    perf_ai_chat_max_tokens = fields.Integer('Chat: max tokens per issue', groups=MANAGER,
                                             config_parameter='perf_monitor.ai_chat_max_tokens',
                                             default=AI_PARAM_DEFAULTS['ai_chat_max_tokens'])
    perf_ai_repo_hint = fields.Char('Repositories (Claude Code prompt)', groups=MANAGER,
                                    config_parameter='perf_monitor.ai_repo_hint', default=DEFAULT_REPO_HINT)
    perf_ai_calls_today = fields.Integer('AI calls today (UTC)', compute='_compute_perf_ai_usage')
    perf_ai_cost_today = fields.Float('AI cost today (USD)', compute='_compute_perf_ai_usage', digits=(12, 4))
    perf_ai_cost_month = fields.Float('AI cost this month (USD)', compute='_compute_perf_ai_usage', digits=(12, 4))
    # thresholds stay stored in milliseconds (the agent and the rules use ms): labelled "(ms)"
    perf_issue_min_ms = fields.Integer('Issue threshold (ms)', config_parameter='perf_monitor.issue_min_ms',
                                       default=DEFAULT_MIN_MS, help='Milliseconds: 2000 ms = 2 s')
    # saving 0 / empty deletes the parameter: perf.issue._issue_thresholds applies the default on read
    perf_issue_min_sql_share = fields.Float('SQL share of the call',
                                            config_parameter='perf_monitor.issue_min_sql_share',
                                            default=DEFAULT_MIN_SQL_SHARE, digits=(3, 2),
                                            help='SQL time / call time (0.5 = 50 %)')
    perf_issue_min_query_ms = fields.Integer('One query (ms)', config_parameter='perf_monitor.issue_min_query_ms',
                                             default=DEFAULT_MIN_QUERY_MS, help='Milliseconds: 1000 ms = 1 s')
    perf_issue_min_repeats = fields.Integer('N+1: same SQL per call', config_parameter='perf_monitor.issue_min_repeats',
                                            default=DEFAULT_MIN_REPEATS)
    perf_issue_min_occ_7d = fields.Integer('Recurring: occurrences in 7 days',
                                           config_parameter='perf_monitor.issue_min_occ_7d', default=DEFAULT_MIN_OCC_7D)
    perf_issue_assignee_id = fields.Many2one('res.users', 'Issue assignee',
                                             config_parameter='perf_monitor.issue_assignee_id',
                                             domain=[('share', '=', False)])
    perf_aws_region = fields.Char('CloudWatch region', config_parameter='perf_monitor.aws_region',
                                  default='eu-west-1')

    @api.depends('company_id')
    def _compute_perf_ai_usage(self):
        Call = self.env['perf.ai.call']
        calls, today, month = Call._calls_today(), Call._cost_today(), Call._cost_month()
        for rec in self:
            rec.perf_ai_calls_today = calls
            rec.perf_ai_cost_today = today
            rec.perf_ai_cost_month = month

    def _perf_stored_number(self, fname):
        """Stored (ir.config_parameter) value of a numeric setting, or None."""
        value = self.env['ir.config_parameter'].sudo().get_param(self._fields[fname].config_parameter)
        try:
            return float(value) if value not in (None, False, '') else None
        except (TypeError, ValueError):
            return None

    @api.constrains(*NON_NEGATIVE)
    def _check_perf_ai_non_negative(self):
        """Only a NEW / CHANGED negative value is refused: a negative value saved before this rule (the form
        accepted it) must not block saving unrelated settings; perf.issue._ai_limits uses the default for it."""
        for rec in self:
            for fname in NON_NEGATIVE:
                value = rec[fname] or 0
                if value < 0 and value != rec._perf_stored_number(fname):
                    raise ValidationError(_('%s cannot be negative (0 / empty = default).')
                                          % rec._fields[fname].string)

    @api.model
    def get_values(self):
        res = super().get_values()
        param = self.env['ir.config_parameter'].sudo()
        for fname in NON_NEGATIVE:
            if (self._perf_stored_number(fname) or 0) < 0:
                _logger.warning('perf_monitor: the setting %s is negative (%s): the default is used; set 0 / a '
                                'positive value in Settings > Performance', self._fields[fname].config_parameter,
                                param.get_param(self._fields[fname].config_parameter))
        if self.env.user.has_group(MANAGER) and not res.get('perf_ai_default_model_id'):
            # not chosen yet: show the model actually used (legacy id match / first row)
            res['perf_ai_default_model_id'] = self.env['perf.ai.model']._default_model().id or False
        for fname, key in ON_BY_DEFAULT.items():
            value = param.get_param('perf_monitor.%s' % key, '')
            res[fname] = True if value in ('', False, None) else str(value).lower() in ('1', 'true', 'yes', 'on')
        return res

    def set_values(self):
        super().set_values()
        if not self.env.user.has_group(MANAGER):
            return
        param = self.env['ir.config_parameter'].sudo()
        for fname, key in ON_BY_DEFAULT.items():
            param.set_param('perf_monitor.%s' % key, 'True' if self[fname] else 'False')
