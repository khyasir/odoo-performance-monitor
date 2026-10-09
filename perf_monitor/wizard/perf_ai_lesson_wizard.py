# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PerfAiLessonWizard(models.TransientModel):
    _name = 'perf.ai.lesson.wizard'
    _description = 'AI solution was wrong: write a lesson'

    issue_id = fields.Many2one('perf.issue', required=True, ondelete='cascade')
    name = fields.Char(compute='_compute_defaults', store=True, readonly=False, required=True)
    lesson = fields.Char('Lesson (one line)', required=True,
                         help='What the AI must do differently next time, e.g. "callsite in write of an _inherit = '
                              '\'base\' class: cache the model names, return super() early"')
    keywords = fields.Char(help='Comma separated; the lesson applies to issues containing one of them')
    regex = fields.Char('Regex (call site / code)', compute='_compute_defaults', store=True, readonly=False)

    @api.depends('issue_id')
    def _compute_defaults(self):
        for wizard in self:
            issue = wizard.issue_id
            wizard.name = (_('Lesson from issue #%s: %s') % (issue.id, issue.name or ''))[:200] if issue else False
            path = (issue.callsite or '').split(':', 1)[0].strip() if issue else ''
            wizard.regex = re.escape(path) if path else False

    def action_confirm(self):
        self.ensure_one()
        issue = self.issue_id
        issue._check_manager()
        text = ' '.join((self.lesson or '').split())
        if not text:
            raise UserError(_('Write the lesson (one line).'))
        if not (self.regex or '').strip() and not (self.keywords or '').strip():
            raise UserError(_('Set keywords or a regex, otherwise the lesson never applies.'))
        lesson = self.env['perf.ai.lesson'].create({
            'name': self.name, 'lesson': text, 'keywords': self.keywords, 'regex': self.regex,
            'issue_id': issue.id, 'pattern': _('Learnt from issue #%s (%s)') % (issue.id, issue.callsite or issue.key),
        })
        issue.write({'ai_feedback': 'wrong', 'ai_feedback_by': self.env.uid, 'ai_feedback_note': text})
        issue._message_log(body=_('AI solution marked wrong, lesson "%s" added: %s') % (lesson.name, text))
        return {'type': 'ir.actions.act_window_close'}
