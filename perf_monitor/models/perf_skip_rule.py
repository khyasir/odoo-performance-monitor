# -*- coding: utf-8 -*-
"""Skip rules: a known slowness is skipped for a while (or forever). Matching slow calls are still
stored and counted on the rule (hit_count / last_hit) but never create, reopen or escalate an issue
and never make an activity. Expiry is checked when a call is matched (no cron needed); the
"Perf Monitor: build issues" cron also archives the expired rules."""
import fnmatch

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

MATCH_TYPES = [
    ('issue_key', 'Issue key'),
    ('fingerprint', 'SQL fingerprint'),
    ('model', 'Model'),
    ('route', 'Route'),
]
# slow call value (perf.agent.slow.call.remote._issue_values() + issue_key) compared per match type
MATCH_VALUE_OF = {'issue_key': 'issue_key', 'fingerprint': 'top_sql_fingerprint', 'model': 'model_name',
                  'route': 'route'}


def skip_rule_matches(match_type, match_value, call):
    """Exact match; a route may use * wildcards (e.g. /web/dataset/*)."""
    value = call.get(MATCH_VALUE_OF.get(match_type)) or ''
    if not match_value or not value:
        return False
    if match_type == 'route' and '*' in match_value:
        return fnmatch.fnmatchcase(value, match_value)
    return value == match_value


class PerfSkipRule(models.Model):
    _name = 'perf.skip.rule'
    _description = 'Performance Skip Rule'
    _order = 'active desc, id desc'

    name = fields.Char(required=True)
    match_type = fields.Selection(MATCH_TYPES, required=True, default='issue_key')
    match_value = fields.Char(required=True, help='Issue key (n1:..., sql:..., method:..., route:...), SQL '
                                                  'fingerprint, model (sale.order) or route (* allowed)')
    until = fields.Datetime(help='Empty: skipped forever. After this date the slowness makes issues again.')
    reason = fields.Text(required=True)
    active = fields.Boolean(default=True)
    user_id = fields.Many2one('res.users', 'Skipped by', default=lambda self: self.env.user, readonly=True)
    hit_count = fields.Integer('Skipped calls', readonly=True,
                               help='Slow calls matched by this rule (stored, no issue made)')
    last_hit = fields.Datetime(readonly=True)
    expired = fields.Boolean(compute='_compute_expired')
    issue_ids = fields.One2many('perf.issue', 'skip_rule_id', 'Issues')

    @api.depends('until')
    def _compute_expired(self):
        now = fields.Datetime.now()
        for rule in self:
            rule.expired = bool(rule.until and rule.until <= now)

    @api.constrains('match_value')
    def _check_match_value(self):
        for rule in self:
            if not (rule.match_value or '').strip():
                raise ValidationError(_('A skip rule needs a value to match.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('match_value'):
                vals['match_value'] = vals['match_value'].strip()
        return super().create(vals_list)

    @api.model
    def _active_rules(self):
        """Active rules not expired yet (the expiry is checked here, at match time)."""
        now = fields.Datetime.now()
        return self.sudo().search(['|', ('until', '=', False), ('until', '>', now)], order='id')

    def _match(self, call):
        """First rule of self matching the slow call values (dict with issue_key), else an empty recordset."""
        for rule in self:
            if skip_rule_matches(rule.match_type, rule.match_value, call):
                return rule
        return self.browse()

    def _register_hits(self, calls):
        self.ensure_one()
        self.sudo().write({'hit_count': self.hit_count + len(calls),
                           'last_hit': max([d for d in calls.mapped('ts') + [self.last_hit] if d])})

    @api.model
    def _archive_expired(self):
        rules = self.sudo().search([('until', '!=', False), ('until', '<=', fields.Datetime.now())])
        rules.write({'active': False})
        return rules
