# -*- coding: utf-8 -*-
"""Per-issue AI chat (Perf Monitor Managers only), shown as a chat pane in the issue form (bubbles rendered
server-side as HTML, styled by static/src/scss/perf_chat.scss; static/src/js/perf_chat.js only adds Enter to
send, auto-grow and scroll: the plain buttons work without it). A bare greeting / thanks gets a canned answer
without a model call (perf_ai_text.chat_smalltalk). Each other question is checked by a cheap guard first
(perf_ai_text.chat_guard): empty / too long / secret or prompt-extraction phrases / clear off-topic without
context get a refusal notice WITHOUT a model call, and the question is put back in the question box. The system
prompt restricts the model to this issue too. Same repo tools (own switch: "Chat tools", Settings > Performance),
same limits and cost log as the analysis (perf_ai.py), plus the per-issue chat limits (answered turns, tokens: all
chats of the issue).

Chats are split in sessions (perf.issue.chat.session, "New chat"): only the current session is replayed to the
model (and only when "Use previous conversation" is on); older sessions stay readable ("Previous chats"). The
model is the chat's own selector, else the issue's AI model, else Settings > Default AI model.

The question box (``ai_chat_draft``) is ONE stored field per issue, shared by every manager: two managers
typing on the same issue at the same time overwrite each other's draft (last save wins). Accepted: the chat
is a single-analyst tool; the sent questions themselves are separate perf.issue.chat.message rows."""
import json
import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .perf_ai import LIMIT_REACHED, SECURITY_RULES, cost_limit_reason, first_call_cost
from .perf_ai_text import MAX_QUESTION_CHARS, REFUSAL_NOTICES, REFUSAL_REASONS, chat_guard, chat_smalltalk, \
    render_chat_html, topic_terms_of
from .perf_ai_tools import wrap_untrusted

_logger = logging.getLogger(__name__)

REFUSAL = ('I can only help with this performance issue: its calls, SQL, code, fix and how to verify it. / '
           'Main sirf is performance issue (calls, SQL, code, fix, verification) ke baare mein madad kar sakta hoon.')
MAX_HISTORY_TURNS = 6


CHAT_SYSTEM_PROMPT = """You are the performance assistant of ONE Odoo 16 performance issue (issue id {issue_id}). \
You only answer questions about THIS issue: its slow calls, SQL, EXPLAIN plan, code, root cause, fix, tests, \
and how to reproduce / measure / verify the fix.
Short follow-ups ("ya bi", "aur samjhao", "why?", "ok test kaise karun") continue the previous conversation: \
connect them to it and answer. If a question is clearly unrelated to this issue, refuse in ONE short sentence \
that says why, e.g. "{refusal}"; never lecture.
Answer in the language of the question (English or Roman Urdu), short and concrete, with a diff when useful \
(in a ```diff code block). Your answer is AI advice and may be wrong: say what must be verified in the repo.
Rules: never propose patching Odoo core; a slow generic hook (`_inherit = 'base'`, AbstractModel, generic \
write / create / unlink) hits every model: cache the small set of relevant model names with tools.ormcache, \
return super() at once for other models, clear the cache when the config records change; if the code is \
incomplete say so and do not guess; show the arithmetic of any expected gain.
{tools}

{security}

Issue context (JSON, SQL without literal values):
{context}
"""


class PerfIssueChatSession(models.Model):
    _name = 'perf.issue.chat.session'
    _description = 'Performance issue AI chat session'
    _order = 'id desc'

    issue_id = fields.Many2one('perf.issue', required=True, ondelete='cascade', index=True, readonly=True)
    number = fields.Integer(readonly=True)
    name = fields.Char(compute='_compute_name', store=True)
    message_ids = fields.One2many('perf.issue.chat.message', 'session_id', 'Messages')

    @api.depends('number', 'create_date', 'message_ids.content', 'message_ids.role')
    def _compute_name(self):
        for session in self:
            first = session.message_ids.filtered(lambda m: m.role == 'user').sorted('id')[:1]
            text = ' '.join((first.content or '').split())
            session.name = _('Chat %(number)s · %(date)s%(question)s') % {
                'number': session.number or '?',
                'date': fields.Datetime.to_string(session.create_date)[:16] if session.create_date else '',
                'question': ' · %s' % (text[:57] + '...' if len(text) > 60 else text) if text else ''}


class PerfIssueChatMessage(models.Model):
    _name = 'perf.issue.chat.message'
    _description = 'Performance issue AI chat message'
    _order = 'id'

    issue_id = fields.Many2one('perf.issue', required=True, ondelete='cascade', index=True, readonly=True)
    session_id = fields.Many2one('perf.issue.chat.session', 'Chat session', ondelete='cascade', index=True,
                                 readonly=True)
    role = fields.Selection([('user', 'Question'), ('assistant', 'AI advice'), ('notice', 'Notice')],
                            required=True, readonly=True)
    content = fields.Text(readonly=True)
    state = fields.Selection([('ok', 'OK'), ('refused', 'Refused'), ('limit', 'Limit reached'), ('error', 'Error')],
                             default='ok', readonly=True)
    refusal_reason = fields.Selection(REFUSAL_REASONS, 'Refusal reason', readonly=True)
    author_id = fields.Many2one('res.users', 'Author', default=lambda self: self.env.user, readonly=True)
    ai_call_id = fields.Many2one('perf.ai.call', 'AI call', readonly=True, ondelete='set null')
    tokens = fields.Integer(readonly=True, help='Input + output tokens of the AI call')

    def _bubble(self):
        """Dict for perf_ai_text.render_chat_html."""
        self.ensure_one()
        call = self.ai_call_id.sudo()
        footnote = model = ''
        if call:
            model = call.ai_model_id.name or call.model_id or '?'
            footnote = '%s · %s in / %s out tokens · %.4f USD' % (call.model_id or '?', call.input_tokens,
                                                                  call.output_tokens, call.cost_usd)
        return {'role': self.role, 'content': self.content or '', 'state': self.state,
                'author': self.author_id.name if self.role == 'user' else '',
                'date': fields.Datetime.to_string(self.create_date)[:16] + ' UTC' if self.create_date else '',
                'model': model, 'footnote': footnote}


class PerfIssueChat(models.Model):
    _inherit = 'perf.issue'

    ai_chat_message_ids = fields.One2many('perf.issue.chat.message', 'issue_id', 'AI chat',
                                          groups='perf_monitor.group_perf_manager')
    ai_chat_draft = fields.Text('Your question', groups='perf_monitor.group_perf_manager', copy=False)
    ai_chat_usage = fields.Char('Chat usage', compute='_compute_ai_chat_usage',
                                groups='perf_monitor.group_perf_manager')
    ai_chat_session_id = fields.Many2one('perf.issue.chat.session', 'Chat', copy=False, ondelete='set null',
                                         groups='perf_monitor.group_perf_manager',
                                         domain="[('issue_id', '=', id)]",
                                         help='Current chat; pick an older one to read it again (and continue it)')
    ai_chat_model_id = fields.Many2one('perf.ai.model', 'Chat model', copy=False, ondelete='set null',
                                       groups='perf_monitor.group_perf_manager',
                                       help='Empty: the AI model of the issue')
    ai_chat_use_history = fields.Boolean('Use previous conversation as context', default=True,
                                         groups='perf_monitor.group_perf_manager',
                                         help='On: the earlier questions / answers of this chat are sent with the '
                                              'question. Off: a fresh context (the issue context is always sent).')
    ai_chat_html = fields.Html('Chat', compute='_compute_ai_chat_html', sanitize=False,
                               groups='perf_monitor.group_perf_manager')

    def _compute_ai_chat_usage(self):
        limits = self._ai_limits()
        for issue in self:
            turns, tokens = issue._ai_chat_used() if isinstance(issue.id, int) else (0, 0)
            issue.ai_chat_usage = _('%(t)d / %(mt)d answers, %(k)d / %(mk)d tokens') % {
                't': turns, 'mt': limits['ai_chat_max_turns'], 'k': tokens, 'mk': limits['ai_chat_max_tokens']}

    @api.depends('ai_chat_session_id', 'ai_chat_message_ids')
    def _compute_ai_chat_html(self):
        for issue in self:
            session = issue.ai_chat_session_id or issue._chat_latest_session()
            msgs = issue._chat_messages(session) if isinstance(issue._origin.id, int) else []
            issue.ai_chat_html = Markup(render_chat_html([m._bubble() for m in msgs]))

    def _ai_chat_used(self):
        """(answered turns, tokens) of the chat of this issue (every session)."""
        self.ensure_one()
        Msg = self.env['perf.issue.chat.message'].sudo()
        turns = Msg.search_count([('issue_id', '=', self.id), ('role', '=', 'assistant'),
                                  ('ai_call_id', '!=', False)])
        calls = self.env['perf.ai.call'].sudo().search([('issue_id', '=', self.id), ('trigger', '=', 'chat')])
        return turns, sum(calls.mapped('input_tokens')) + sum(calls.mapped('output_tokens'))

    def _chat_latest_session(self):
        issue_id = self._origin.id if isinstance(self._origin.id, int) else 0
        return self.env['perf.issue.chat.session'].sudo().search([('issue_id', '=', issue_id)], order='id desc',
                                                                 limit=1)

    def _chat_session(self):
        """Current session (the selected one, else the latest, else a new one)."""
        self.ensure_one()
        session = self.sudo().ai_chat_session_id or self._chat_latest_session()
        if not session:
            session = self._chat_new_session()
        elif self.sudo().ai_chat_session_id != session:
            self.sudo().ai_chat_session_id = session
        return session

    def _chat_new_session(self):
        Session = self.env['perf.issue.chat.session'].sudo()
        last = Session.search([('issue_id', '=', self.id)], order='number desc', limit=1)
        session = Session.create({'issue_id': self.id, 'number': (last.number or 0) + 1})
        self.sudo().ai_chat_session_id = session
        return session

    def _chat_messages(self, session):
        """Messages of ``session``; messages without session (stored before 16.0.1.12.0, when the migration did
        not run) belong to the first session."""
        Msg = self.env['perf.issue.chat.message'].sudo()
        if not session:
            return Msg.search([('issue_id', '=', self._origin.id), ('session_id', '=', False)], order='id')
        domain = [('issue_id', '=', session.issue_id.id), ('session_id', '=', session.id)]
        if session.number == 1:
            domain = ['&', ('issue_id', '=', session.issue_id.id), '|', ('session_id', '=', session.id),
                      ('session_id', '=', False)]
        return Msg.search(domain, order='id')

    def _chat_topic_terms(self):
        terms = topic_terms_of((self.model_name, self.method, self.cron_name, self.route, self.callsite, self.menu,
                                self.dashboard, self.tile, self.name))
        terms.update(self._plan_tables())
        return terms

    def _chat_system_prompt(self, limits):
        context = self._ai_context(limits)
        if limits['ai_chat_tools_enabled']:
            tools = 'You may use the read-only tools (read_file, grep, list_module, find_symbol) for code of this ' \
                    'issue only.'
        else:
            tools = 'No tools are available in this chat: answer from the context below; say which file / line ' \
                    'must be checked when the context is not enough.'
        if self.ai_state == 'done':
            # model output that read the untrusted issue fields: framed too
            context['previous_ai_analysis_may_be_wrong'] = wrap_untrusted('previous AI analysis', json.dumps({
                'root_cause': self.ai_root_cause or '', 'fix': (self.ai_fix or '')[:4000],
                'expected_impact': self.ai_expected_impact or ''}, ensure_ascii=False))
        return CHAT_SYSTEM_PROMPT.format(
            issue_id=self.id, refusal=REFUSAL, security=SECURITY_RULES, tools=tools,
            context=json.dumps(context, indent=1, ensure_ascii=False, default=str))

    def _chat_history(self, session=None):
        """Earlier answered turns (question + answer) of ``session`` (default: the current one), oldest first,
        as Converse messages (the last MAX_HISTORY_TURNS). They are replayed as plain conversation turns, NOT
        framed as untrusted data: the questions were typed by a Perf Monitor Manager (and passed chat_guard), the
        answers are this chat's own earlier model output. That output may repeat text of untrusted data it read
        (tool results, issue fields); it is not re-framed because Converse needs alternating user / assistant
        turns with the assistant's own text, and SECURITY_RULES in the system prompt apply to the whole
        conversation. The issue's analysis (another run) is framed, see previous_ai_analysis_may_be_wrong."""
        session = session if session is not None else self._chat_session()
        pairs, question = [], None
        for msg in self._chat_messages(session):
            if msg.role == 'user':
                question = msg if msg.state != 'refused' else None
            elif msg.role == 'assistant' and msg.state == 'ok' and question is not None and msg.ai_call_id:
                pairs.append((question.content, msg.content))
                question = None
        messages = []
        for q, a in pairs[-MAX_HISTORY_TURNS:]:
            messages.append({'role': 'user', 'content': [{'text': q}]})
            messages.append({'role': 'assistant', 'content': [{'text': a or '-'}]})
        return messages

    def action_ai_chat_new(self):
        """"New chat": a fresh session (the current one is reused while it is still empty)."""
        self.ensure_one()
        self._check_manager()
        current = self.sudo().ai_chat_session_id or self._chat_latest_session()
        if current and not self._chat_messages(current):
            self.sudo().ai_chat_session_id = current
        else:
            self._chat_new_session()
        return True

    def action_ai_chat_send(self):
        self.ensure_one()
        self._check_manager()
        question = (self.ai_chat_draft or '').strip()
        if not question:
            raise UserError(_('Write a question first.'))
        limits = self._ai_limits()
        if not self._ai_enabled() or not limits['ai_chat_enabled']:
            raise UserError(_('The AI chat is disabled in Settings > Performance.'))
        Msg = self.env['perf.issue.chat.message']
        session = self._chat_session()
        base = {'issue_id': self.id, 'session_id': session.id}
        smalltalk = chat_smalltalk(question)
        if smalltalk:
            # "hi" / "shukriya": a canned answer, no model call, no cost, not counted as an answered turn
            Msg.create(dict(base, role='user', content=question))
            Msg.create(dict(base, role='assistant', content=smalltalk, state='ok'))
            self.ai_chat_draft = False
            return True
        history = self._chat_history(session) if self.ai_chat_use_history else []
        ok, why = chat_guard(question, self._chat_topic_terms(), has_history=bool(history))
        if not ok:
            # logged, no model call; the question goes back into the box so that it need not be typed again
            Msg.create(dict(base, role='user', content=question[:MAX_QUESTION_CHARS * 2], state='refused',
                            refusal_reason=why))
            Msg.create(dict(base, role='notice', content=REFUSAL_NOTICES.get(why, REFUSAL), state='refused',
                            refusal_reason=why))
            self.ai_chat_draft = question
            return True
        system = error = ''
        conf = self._ai_conf(self.sudo().ai_chat_model_id or None)
        # built BEFORE the question is stored: a failure (pg_class query, file read, lesson regex) becomes
        # an error notice in the chat, not a traceback after the draft was cleared
        try:
            run_limits, timing = self._ai_time_caps('chat', limits)
            system = self._chat_system_prompt(run_limits)
        except Exception as exc:  # pylint: disable=broad-except
            error = ('%s: %s' % (type(exc).__name__, exc))[:500]
            _logger.warning('perf_monitor: chat prompt of issue %s failed: %s', self.id, error)
        Msg.create(dict(base, role='user', content=question[:MAX_QUESTION_CHARS * 2]))
        self.ai_chat_draft = False
        if error:
            Msg.create(dict(base, role='notice', state='error',
                            content=_('The issue context could not be built, no AI call was made: %s') % error))
            return True
        turns, tokens = self._ai_chat_used()
        blocked = ''
        if turns >= limits['ai_chat_max_turns']:
            blocked = _('%s: chat answers for this issue (%d)') % (LIMIT_REACHED, limits['ai_chat_max_turns'])
        elif tokens >= limits['ai_chat_max_tokens']:
            blocked = _('%s: chat tokens for this issue (%d)') % (LIMIT_REACHED, limits['ai_chat_max_tokens'])
        cost_left = 0.0
        if not blocked:
            blocked, cost_left, _kind = self._ai_budget(limits)
        messages = history + [{'role': 'user', 'content': [{'text': question}]}]
        if not blocked and first_call_cost(system, messages, run_limits, conf['price_in'], conf['price_out']) \
                > cost_left:
            # the first call alone could pass the budget: a notice, no perf.ai.call row (no model call)
            blocked = '%s: %s' % (LIMIT_REACHED, cost_limit_reason(cost_left))
        if blocked:
            Msg.create(dict(base, role='notice', content=blocked, state='limit'))
            return True
        max_input = min(limits['ai_max_input_tokens'], limits['ai_chat_max_tokens'] - tokens)
        run, log = self._ai_run('chat', system, messages, run_limits, cost_left, conf=conf,
                                max_input_tokens=max_input, timing=timing, chat=True)
        used = run['input_tokens'] + run['output_tokens']
        if run['state'] == 'ok':
            Msg.create(dict(base, role='assistant', content=run['text'] or '-', state='ok', ai_call_id=log.id,
                            tokens=used))
        else:
            reason = run['limit_reason'] or run['error']
            content = '\n\n'.join(p for p in (run['text'], '[%s]' % reason) if p)
            Msg.create(dict(base, role='assistant', content=content, state=run['state'], ai_call_id=log.id,
                            tokens=used))
        return True
