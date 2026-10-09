# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .. import runtime
from .perf_agent_slow_call import _RX_DB, is_allowed_central_url

PREFIX = 'perf_monitor_agent.'
_RX_TOKEN = re.compile(r'^[\x21-\x7e]{16,256}$')  # printable ASCII, no spaces (an X-Perf-Token header value)


class ResConfigSettings(models.TransientModel):
    """Settings > Perf Monitor Agent (administrators only). The central token is write-only: the form
    never receives the stored value (only its last 4 characters); leaving it empty keeps it."""
    _inherit = 'res.config.settings'

    perf_agent_enabled = fields.Boolean('Perf Monitor Agent enabled', groups='base.group_system',
                                        help='Kill switch: off = no tagging, no slow call recorded')
    perf_agent_slow_ms = fields.Integer('Slow call threshold (ms)', groups='base.group_system')
    perf_agent_central_url = fields.Char('Central URL', groups='base.group_system',
                                         help='https:// URL of the central perf_monitor (plain http:// only to '
                                              '127.0.0.1 / [::1] / localhost)')
    perf_agent_central_db = fields.Char('Central database', groups='base.group_system')
    perf_agent_central_token = fields.Char('New central token', groups='base.group_system',
                                           help='Token of this server in the central perf_monitor. Leave empty '
                                                'to keep the current one.')
    perf_agent_token_hint = fields.Char('Current token', readonly=True, groups='base.group_system')
    perf_agent_collect_enabled = fields.Boolean(
        'Collect DB metrics', groups='base.group_system',
        help='"Perf Monitor: agent collect" (every 5 minutes) pushes DB latency, Top SQL (pg_stat_statements, '
             'skipped when the extension is missing) and live / blocked queries of this database to the central '
             'perf_monitor, with the central token above: no root collector needed.')
    perf_agent_send_host_metrics = fields.Boolean(
        'Send host metrics', groups='base.group_system',
        help='Also send CPU / RAM / disk / load / network of this host. Enable it in ONLY ONE database per '
             'host (the values are the same for every database of the host and would be duplicated); the '
             'other databases still send their DB latency, Top SQL and live queries.')

    @api.model
    def get_values(self):
        res = super().get_values()
        if not self.env.user._is_system():
            return res
        params = self.env['ir.config_parameter'].sudo()
        enabled = (params.get_param(PREFIX + 'enabled') or '1').strip().lower()
        collect = (params.get_param(PREFIX + 'collect_enabled') or '1').strip().lower()
        send_host = (params.get_param(PREFIX + 'send_host_metrics') or '1').strip().lower()
        try:
            slow_ms = int(params.get_param(PREFIX + 'slow_ms') or runtime.DEFAULT_SLOW_MS)
        except (TypeError, ValueError):
            slow_ms = runtime.DEFAULT_SLOW_MS
        token = (params.get_param(PREFIX + 'central_token') or '').strip()
        res.update(
            perf_agent_enabled=enabled not in ('0', 'false', 'no', 'off'),
            perf_agent_slow_ms=slow_ms,
            perf_agent_central_url=params.get_param(PREFIX + 'central_url') or False,
            perf_agent_central_db=params.get_param(PREFIX + 'central_db') or False,
            perf_agent_central_token=False,
            perf_agent_token_hint=('set (ends with ...%s)' % token[-4:]) if token else 'not set',
            perf_agent_collect_enabled=collect not in ('0', 'false', 'no', 'off'),
            perf_agent_send_host_metrics=send_host not in ('0', 'false', 'no', 'off'),
        )
        return res

    def set_values(self):
        super().set_values()
        if not self.env.user._is_system():
            return
        url = (self.perf_agent_central_url or '').strip().rstrip('/')
        if url and not is_allowed_central_url(url):
            raise ValidationError(_('Central URL must be https:// (plain http:// only to 127.0.0.1, [::1] or '
                                    'localhost).'))
        db = (self.perf_agent_central_db or '').strip()
        if db and not _RX_DB.match(db):
            raise ValidationError(_('Invalid central database name.'))
        if self.perf_agent_slow_ms < 0:
            raise ValidationError(_('The slow call threshold must be >= 0 ms.'))
        token = (self.perf_agent_central_token or '').strip()
        if token and not _RX_TOKEN.match(token):
            raise ValidationError(_('The central token must be 16-256 printable characters without spaces.'))
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(PREFIX + 'enabled', '1' if self.perf_agent_enabled else '0')
        params.set_param(PREFIX + 'slow_ms', str(self.perf_agent_slow_ms))
        params.set_param(PREFIX + 'collect_enabled', '1' if self.perf_agent_collect_enabled else '0')
        params.set_param(PREFIX + 'send_host_metrics', '1' if self.perf_agent_send_host_metrics else '0')
        params.set_param(PREFIX + 'central_url', url or False)
        params.set_param(PREFIX + 'central_db', db or False)
        if token:
            params.set_param(PREFIX + 'central_token', token)
        runtime.clear_config_cache()  # this process; the other workers within CONFIG_TTL seconds
