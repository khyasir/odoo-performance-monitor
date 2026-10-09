# -*- coding: utf-8 -*-
"""Lessons library: short rules learnt from wrong AI answers ("AI solution was wrong" wizard). The
lessons whose keywords / regex match an issue (title, key, call site, model, method, SQL pattern, code
around the call site) are injected into the AI prompt, the chat and the Claude Code prompt."""
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .perf_issue import strip_sql


class PerfAiLesson(models.Model):
    _name = 'perf.ai.lesson'
    _description = 'AI lesson (learnt from a wrong AI answer)'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    pattern = fields.Text('When it applies', help='Plain description of the pattern this lesson is about')
    keywords = fields.Char(help='Comma separated, case-insensitive; the lesson applies when one of them appears in '
                                'the issue (title, key, call site, model, method, SQL pattern, code)')
    regex = fields.Char('Regex (call site / code)', help='Python regular expression searched in the same text '
                                                         '(multi-line, case-insensitive)')
    lesson = fields.Text(required=True, help='Short instruction given to the AI and to Claude Code')
    issue_id = fields.Many2one('perf.issue', 'Learnt from issue', ondelete='set null', readonly=True)

    @api.constrains('name', 'regex', 'keywords')
    def _check_match(self):
        for lesson in self:
            if not (lesson.regex or '').strip() and not lesson._keyword_list():
                raise ValidationError(_('Lesson "%s": set keywords or a regex, otherwise it never applies.')
                                      % lesson.name)
            if lesson.regex:
                try:
                    re.compile(lesson.regex)
                except re.error as exc:
                    raise ValidationError(_('Lesson "%s": invalid regex: %s') % (lesson.name, exc))

    def _keyword_list(self):
        return [k.strip().lower() for k in re.split(r'[,\n]', self.keywords or '') if k.strip()]

    def _matches(self, haystack):
        self.ensure_one()
        lower = haystack.lower()
        if any(k in lower for k in self._keyword_list()):
            return True
        if self.regex:
            try:
                return bool(re.search(self.regex, haystack, re.I | re.M))
            except re.error:
                return False
        return False

    @api.model
    def _issue_haystack(self, issue, code=None):
        if code is None:
            code = issue._ai_code()
        return '\n'.join(p for p in (issue.name, issue.key, issue.callsite, issue.model_name, issue.method,
                                     issue.cron_name, issue.route, strip_sql(issue.top_sql), code) if p)

    @api.model
    def _match_issue(self, issue, code=None):
        """Active lessons that apply to this issue (``code``: the code around the call site, if known)."""
        haystack = self._issue_haystack(issue, code)
        return self.search([]).filtered(lambda lesson: lesson._matches(haystack))
