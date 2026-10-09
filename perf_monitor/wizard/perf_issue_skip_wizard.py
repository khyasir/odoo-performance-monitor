# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.perf_skip_rule import MATCH_TYPES

DURATIONS = [('7d', '7 days'), ('30d', '30 days'), ('forever', 'Forever')]
DURATION_DAYS = {'7d': 7, '30d': 30}
# perf.issue field giving the default match value per match type
ISSUE_FIELD_OF = {'issue_key': 'key', 'fingerprint': 'top_sql_fingerprint', 'model': 'model_name', 'route': 'route'}


class PerfIssueSkipWizard(models.TransientModel):
    _name = 'perf.issue.skip.wizard'
    _description = 'Skip a performance issue'

    issue_id = fields.Many2one('perf.issue', required=True, ondelete='cascade')
    match_type = fields.Selection(MATCH_TYPES, required=True, default='issue_key')
    match_value = fields.Char(compute='_compute_match_value', store=True, readonly=False)
    duration = fields.Selection(DURATIONS, required=True, default='7d')
    reason = fields.Text(required=True)

    @api.depends('issue_id', 'match_type')
    def _compute_match_value(self):
        for wizard in self:
            fname = ISSUE_FIELD_OF.get(wizard.match_type)
            wizard.match_value = (wizard.issue_id[fname] if wizard.issue_id and fname else False) or False

    def action_confirm(self):
        self.ensure_one()
        issue = self.issue_id
        issue._check_manager()
        value = (self.match_value or '').strip()
        if not value:
            raise UserError(_('This issue has no %s: choose another match type or type the value.')
                            % dict(MATCH_TYPES)[self.match_type])
        if not (self.reason or '').strip():
            raise UserError(_('Write why this slowness is skipped.'))
        days = DURATION_DAYS.get(self.duration)
        until = fields.Datetime.now() + timedelta(days=days) if days else False
        rule = self.env['perf.skip.rule'].create({
            'name': _('Skip %s') % (issue.name or value)[:200], 'match_type': self.match_type, 'match_value': value,
            'until': until, 'reason': self.reason,
        })
        issue.write({'state': 'skipped', 'skip_rule_id': rule.id})
        issue.sudo().activity_ids.unlink()  # like Close: dropped, not "done"
        issue._message_log(body=_('Skipped %(until)s by %(user)s (%(type)s = %(value)s): %(reason)s',
                                  until=_('until %s') % until if until else _('forever'), user=self.env.user.name,
                                  type=dict(MATCH_TYPES)[self.match_type], value=value, reason=self.reason))
        return {'type': 'ir.actions.act_window_close'}
