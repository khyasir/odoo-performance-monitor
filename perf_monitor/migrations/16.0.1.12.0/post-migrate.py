# -*- coding: utf-8 -*-
"""16.0.1.12.0 (idempotent):
- AI models table: the default AI model is the row of the old free-text perf_monitor.ai_model_id (created when
  missing) with the old region / prices, so that existing installs keep the same model and cost; an old "deep
  analyse" model id becomes a row too (the Deep analyse button is gone: pick it as the issue's AI model);
- issues keep an empty AI model (= Settings > Default AI model at run time, as for new issues);
- chat sessions: the chat messages stored before get session 1 of their issue;
- call sites: issues whose call site is wags_logger's cursor patch (or another skipped frame) get the first real
  application frame of their slow calls when one is stored; otherwise they are left as they are;
- the new stored AI usage fields (tokens, cost, last model) are computed."""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.perf_monitor.models.perf_ai import DEFAULT_MODEL_ID, DEFAULT_PRICE_INPUT_MTOK, \
    DEFAULT_PRICE_OUTPUT_MTOK, DEFAULT_REGION
from odoo.addons.perf_monitor.models.perf_ai_model import DEFAULT_MODEL_PARAM, LEGACY_MODEL_PARAM
from odoo.addons.perf_monitor.models.perf_ai_text import best_callsite, real_callsite

_logger = logging.getLogger(__name__)

OLD_DEEP_PRICES = (5.0, 25.0)   # defaults of the removed "Deep analyse" price settings


def _float(value, default):
    try:
        return float(value) if value not in (None, False, '') else default
    except (TypeError, ValueError):
        return default


def _ai_models(env):
    ICP = env['ir.config_parameter'].sudo()
    Model = env['perf.ai.model'].with_context(active_test=False)
    region = (ICP.get_param('perf_monitor.ai_region') or '').strip() or DEFAULT_REGION
    for xmlid in ('perf_monitor.ai_model_haiku_4_5', 'perf_monitor.ai_model_opus_5_5'):
        seeded = env.ref(xmlid, raise_if_not_found=False)
        if seeded and seeded.region == DEFAULT_REGION and region != DEFAULT_REGION:
            seeded.region = region
    current = ICP.get_param(DEFAULT_MODEL_PARAM)
    try:
        linked = Model.browse(int(current)).exists() if current else Model
    except (TypeError, ValueError):
        linked = Model
    if not linked:
        legacy = (ICP.get_param(LEGACY_MODEL_PARAM) or '').strip() or DEFAULT_MODEL_ID
        vals = {'region': region,
                'price_input_mtok': _float(ICP.get_param('perf_monitor.ai_price_input_mtok'), DEFAULT_PRICE_INPUT_MTOK),
                'price_output_mtok': _float(ICP.get_param('perf_monitor.ai_price_output_mtok'),
                                            DEFAULT_PRICE_OUTPUT_MTOK)}
        linked = Model.search([('bedrock_model_id', '=', legacy)], limit=1)
        if linked:
            linked.write(dict(vals, active=True))
        else:
            linked = Model.create(dict(vals, name=legacy, bedrock_model_id=legacy, sequence=5))
        ICP.set_param(DEFAULT_MODEL_PARAM, str(linked.id))
        _logger.info('perf_monitor: default AI model = %s (from %s)', linked.bedrock_model_id, LEGACY_MODEL_PARAM)
    deep = (ICP.get_param('perf_monitor.ai_deep_model_id') or '').strip()
    if deep and not Model.search([('bedrock_model_id', '=', deep)], limit=1):
        Model.create({
            'name': deep, 'bedrock_model_id': deep, 'region': region, 'sequence': 30,
            'price_input_mtok': _float(ICP.get_param('perf_monitor.ai_deep_price_input_mtok'), OLD_DEEP_PRICES[0]),
            'price_output_mtok': _float(ICP.get_param('perf_monitor.ai_deep_price_output_mtok'), OLD_DEEP_PRICES[1])})
        _logger.info('perf_monitor: the old deep analyse model %s is now an AI model row', deep)


def _chat_sessions(env):
    env.cr.execute('SELECT DISTINCT issue_id FROM perf_issue_chat_message WHERE session_id IS NULL')
    issue_ids = [r[0] for r in env.cr.fetchall()]
    Session, Msg = env['perf.issue.chat.session'], env['perf.issue.chat.message']
    for issue in env['perf.issue'].with_context(active_test=False).browse(issue_ids).exists():
        session = Session.search([('issue_id', '=', issue.id), ('number', '=', 1)], limit=1) or \
            Session.create({'issue_id': issue.id, 'number': 1})
        Msg.search([('issue_id', '=', issue.id), ('session_id', '=', False)]).write({'session_id': session.id})
        if not issue.ai_chat_session_id:
            issue.ai_chat_session_id = session
    if issue_ids:
        _logger.info('perf_monitor: chat messages of %d issue(s) moved to their first chat session', len(issue_ids))


def _callsites(env):
    issues = env['perf.issue'].with_context(active_test=False).search([('callsite', '!=', False)])
    fixed = left = 0
    for issue in issues.filtered(lambda i: not real_callsite(i.callsite)):
        env.cr.execute("""
            SELECT top_sql_callsite FROM perf_agent_slow_call_remote
             WHERE issue_id = %s AND top_sql_callsite IS NOT NULL
             ORDER BY CASE WHEN kind = 'tile' THEN COALESCE(NULLIF(tile_ms, 0), duration_ms, 0)
                           ELSE COALESCE(duration_ms, 0) END DESC, id DESC
        """, (issue.id,))
        found = best_callsite(r[0] for r in env.cr.fetchall())
        if not found:
            left += 1
            continue
        vals = {'callsite': found}
        if issue.callsite in (issue.name or ''):
            vals['name'] = issue.name.replace(issue.callsite, found)[:256]
        # no chatter tracking message for this repair (name is tracked)
        issue.with_context(tracking_disable=True, mail_notrack=True).write(vals)
        fixed += 1
    if fixed or left:
        _logger.info('perf_monitor: call site of %d issue(s) recomputed from their slow calls (skipped wags_logger / '
                     'SQL layer frames); %d left unchanged (no real frame stored)', fixed, left)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    _ai_models(env)
    _chat_sessions(env)
    _callsites(env)
    Issue = env['perf.issue'].with_context(active_test=False)
    issues = Issue.search([])
    for fname in ('ai_tokens_in', 'ai_tokens_out', 'ai_cost_usd', 'ai_last_model'):
        env.add_to_compute(Issue._fields[fname], issues)
    issues.flush_recordset(['ai_tokens_in', 'ai_tokens_out', 'ai_cost_usd', 'ai_last_model'])
