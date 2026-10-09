# -*- coding: utf-8 -*-
import json
import os
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models import perf_ai
from .common_ai import FakeBedrock, answer, fake_bedrock, tool_use

ANSWER = json.dumps({
    'explanation_en': 'Every write of every model runs two searches.',
    'explanation_roman_urdu': 'Har model ki har write par do searches chal rahi hain.',
    'root_cause': 'Generic write override on base.', 'fix': 'cache the model names', 'index_sql': '',
    'index_justification': 'No index needed.', 'expected_impact': '2 queries per write -> 0',
    'risk': 'Cache invalidation.'})
LIMITS = dict(perf_ai.AI_PARAM_DEFAULTS)
HERE = os.path.dirname(os.path.abspath(__file__))


def _sandbox(**kw):
    from ..models.perf_ai_tools import RepoSandbox  # pylint: disable=import-outside-toplevel
    return RepoSandbox([os.path.dirname(os.path.dirname(HERE))], **kw)


@tagged('post_install', '-at_install')
class TestConverseLoop(TransactionCase):
    """run_converse: tool rounds, files, tokens, output, cost; stops at once with the partial answer."""

    def _run(self, script, limits=None, cost_left=5.0, sandbox=True, messages=None, **kw):
        client = FakeBedrock(script)
        run = perf_ai.run_converse(
            client, 'model-x', 'system', messages or [{'role': 'user', 'content': [{'text': 'analyse'}]}],
            dict(LIMITS, **(limits or {})), cost_left, 1.0, 5.0, sandbox=_sandbox() if sandbox else None, **kw)
        return run, client

    def test_tool_round_then_answer(self):
        run, client = self._run([
            tool_use([('read_file', {'path': 'perf_monitor/tests/fixtures/sample_base_override.py', 'start': 8,
                                     'end': 20})], text='Let me read the class.'),
            answer(ANSWER)])
        self.assertEqual((run['state'], run['rounds'], run['tool_calls']), ('ok', 2, 1))
        self.assertEqual(run['files'], ['perf_monitor/tests/fixtures/sample_base_override.py'])
        self.assertEqual((run['input_tokens'], run['output_tokens']), (2000, 300))
        self.assertEqual(run['text'], ANSWER)
        self.assertEqual(client.requests[0]['toolConfig']['tools'][0]['toolSpec']['name'], 'read_file')
        second = client.requests[1]['messages']
        self.assertEqual([m['role'] for m in second], ['user', 'assistant', 'user'])
        result = second[2]['content'][0]['toolResult']
        self.assertEqual(result['status'], 'success')
        self.assertIn('<<<UNTRUSTED DATA', result['content'][0]['text'])
        self.assertIn("_inherit = 'base'", result['content'][0]['text'])
        self.assertIn('budget left: 7 model call(s), 19 file read(s)', second[2]['content'][1]['text'])

    def test_unknown_blocks_are_not_sent_back(self):
        """An old boto3 returns blocks of newer models as SDK_UNKNOWN_MEMBER: sending them back fails the next call."""
        first = tool_use([('list_module', {'name': 'perf_monitor'})], text='looking')
        first['output']['message']['content'].insert(0, {'SDK_UNKNOWN_MEMBER': {'name': 'newBlock'}})
        run, client = self._run([first, answer(ANSWER)])
        self.assertEqual(run['state'], 'ok')
        sent = client.requests[1]['messages'][1]['content']
        self.assertFalse([b for b in sent if 'SDK_UNKNOWN_MEMBER' in b])
        self.assertTrue([b for b in sent if 'toolUse' in b], 'tool use must stay')
        self.assertEqual(perf_ai.clean_content([{'text': 'a'}, {}, {'SDK_UNKNOWN_MEMBER': {}}]), [{'text': 'a'}])

    def test_refused_tool_goes_back_as_error(self):
        run, client = self._run([tool_use([('read_file', {'path': '../../etc/passwd'})]), answer(ANSWER)])
        self.assertEqual(run['state'], 'ok')
        result = client.requests[1]['messages'][2]['content'][0]['toolResult']
        self.assertEqual(result['status'], 'error')
        self.assertIn('refused', result['content'][0]['text'])
        self.assertEqual(run['files'], [])

    def test_max_rounds(self):
        run, client = self._run([tool_use([('list_module', {'name': 'perf_monitor'})], text='partial thought')],
                                limits={'ai_max_tool_rounds': 3})
        self.assertEqual((run['state'], run['rounds'], len(client.requests)), ('limit', 3, 3))
        self.assertIn('limit reached: max model calls / tool rounds (3)', run['limit_reason'])
        self.assertIn('partial thought', run['text'])
        self.assertIn('LAST CALL', client.requests[2]['messages'][-1]['content'][-1]['text'])

    def test_max_files(self):
        path = 'perf_monitor/tests/fixtures/sample_base_override.py'
        run, client = self._run([tool_use([('read_file', {'path': path}),
                                           ('read_file', {'path': 'perf_monitor/__manifest__.py'})])],
                                limits={'ai_max_files_read': 1})
        self.assertEqual(run['state'], 'limit')
        self.assertIn('max files read (1)', run['limit_reason'])
        self.assertEqual(run['files'], [path])
        self.assertEqual(len(client.requests), 1, 'stopped at once, no further model call')

    def test_input_tokens(self):
        # the usage reported by the model counts: the next round would pass the limit
        run, client = self._run([tool_use([('list_module', {'name': 'perf_monitor'})], tin=9990)],
                                limits={'ai_max_input_tokens': 10000})
        self.assertEqual((run['state'], len(client.requests)), ('limit', 1))
        self.assertIn('max input tokens (10000', run['limit_reason'])
        # a prompt bigger than the limit is never sent
        big = [{'role': 'user', 'content': [{'text': 'x' * 40000}]}]
        run, client = self._run([answer(ANSWER)], limits={'ai_max_input_tokens': 5000}, messages=big)
        self.assertEqual((run['state'], run['rounds'], client.requests), ('limit', 0, []))

    def test_output_tokens_and_cost(self):
        run, _client = self._run([answer('{"explanation_en": "cut', stop='max_tokens')])
        self.assertEqual(run['state'], 'limit')
        self.assertIn('max output tokens', run['limit_reason'])
        self.assertIn('cut', run['text'])
        self.assertFalse(run['budget_stop'])
        run, client = self._run([answer(ANSWER)], cost_left=0.001)
        self.assertEqual((run['state'], client.requests), ('limit', []))
        self.assertIn('cost limit', run['limit_reason'])
        self.assertTrue(run['budget_stop'], 'flag, not the message text')

    def test_tool_calls_per_turn(self):
        uses = [('grep', {'pattern': 'def write', 'path_glob': 'perf_monitor/tests/fixtures/*.py'})] * 11
        run, client = self._run([tool_use(uses), answer(ANSWER)])
        self.assertEqual((run['state'], run['tool_calls']), ('ok', perf_ai.MAX_TOOL_CALLS_PER_TURN))
        results = [b['toolResult'] for b in client.requests[1]['messages'][2]['content'] if 'toolResult' in b]
        self.assertEqual(len(results), 11, 'one result per toolUseId')
        self.assertEqual([r['status'] for r in results], ['success'] * 8 + ['error'] * 3)
        self.assertIn('too many tool calls in one turn (max 8)', results[-1]['content'][0]['text'])

    def test_deadline_and_on_round(self):
        rounds = []
        run, client = self._run([tool_use([('list_module', {'name': 'perf_monitor'})]), answer(ANSWER)],
                                on_round=lambda res: rounds.append((res['rounds'], res['input_tokens'])))
        self.assertEqual(rounds, [(1, 1000), (2, 2000)])
        # no model call may start when it could end after the deadline
        run, client = self._run([answer(ANSWER)], deadline=time.monotonic() + 5, round_seconds=10)
        self.assertEqual((run['state'], client.requests), ('limit', []))
        self.assertIn('time budget of this run', run['limit_reason'])
        # the retry without temperature is a model call too: not started after the deadline
        clock = [1000.0]

        def refuse_slowly(request, n):
            clock[0] += 60
            return RuntimeError('An error occurred (ValidationException) when calling the Converse operation: '
                                'temperature is deprecated for this model.')
        with patch.object(perf_ai.time, 'monotonic', side_effect=lambda: clock[0]):
            run, client = self._run(refuse_slowly, deadline=1050.0, round_seconds=10)
        self.assertEqual((run['state'], len(client.requests), run['temperature_dropped']), ('limit', 1, True))
        self.assertIn('retry without temperature', run['limit_reason'])

    def test_tools_disabled_and_error(self):
        run, client = self._run([answer(ANSWER)], sandbox=False)
        self.assertNotIn('toolConfig', client.requests[0])
        self.assertEqual(run['state'], 'ok')
        run, _client = self._run([RuntimeError('ThrottlingException')])
        self.assertEqual(run['state'], 'error')
        self.assertIn('ThrottlingException', run['error'])


@tagged('post_install', '-at_install')
class TestAiIssueLimits(TransactionCase):
    """Limits applied to the issue analysis: partial answer + 'limit reached', daily calls / cost, monthly cost,
    AI model per issue (prices, temperature), every run logged with its input."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Issue = cls.env['perf.issue']
        cls.Issue.search([]).write({'ai_state': 'skipped'})
        cls.param = cls.env['ir.config_parameter']
        cls.param.set_param('perf_monitor.ai_enabled', 'True')
        cls.issue = cls.Issue.create({
            'name': 'Slow write', 'fingerprint': 'f' * 40, 'key': 'method:x.y.write', 'issue_type': 'slow_method',
            'model_name': 'x.y', 'method': 'write', 'ai_state': 'pending', 'max_duration_ms': 12345.0,
            'callsite': 'perf_monitor/tests/fixtures/sample_base_override.py:17 (write)'})

    def _log(self, **vals):
        return self.env['perf.ai.call'].create(dict({'status': 'ok', 'trigger': 'auto'}, **vals))

    def test_limit_keeps_partial_answer(self):
        self.param.set_param('perf_monitor.ai_max_tool_rounds', '2')
        script = [tool_use([('read_file', {'path': 'perf_monitor/tests/fixtures/sample_base_override.py'})],
                           text='The hook runs for every model')]
        with fake_bedrock(self.env, script) as client:
            self.assertFalse(self.issue._ai_analyse('auto'))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(self.issue.ai_state, 'limit')
        self.assertIn('limit reached', self.issue.ai_error)
        self.assertIn('The hook runs for every model', self.issue.ai_partial)
        log = self.issue.ai_call_ids
        self.assertEqual((log.status, log.rounds, log.files_read, log.tool_calls), ('limit', 2, 1, 1))
        self.assertIn('sample_base_override.py', log.files)
        self.assertAlmostEqual(log.cost_usd, (2000 * 1.0 + 200 * 5.0) / 1e6)
        # the prompt sent: code with class header, lessons, SQL-free context, the system rules
        first = client.requests[0]
        self.assertIn("_inherit = 'base'", first['messages'][0]['content'][0]['text'])
        self.assertIn('at most 2 model calls', first['system'][0]['text'])

    def test_daily_calls_cost_and_month(self):
        self.param.set_param('perf_monitor.ai_daily_limit', '3')
        self._log(cost_usd=1.0)
        self.assertEqual(self.Issue._ai_budget()[0], '')
        self.assertAlmostEqual(self.Issue._ai_budget()[1], 4.0)   # default 5 USD / day
        self._log(cost_usd=4.5)
        blocked = self.Issue._ai_budget()[0]
        self.assertIn('limit reached: daily AI cost', blocked)
        with fake_bedrock(self.env, [answer(ANSWER)]) as client:
            self.assertEqual(self.Issue._ai_analyse_pending(), 0)
            with self.assertRaisesRegex(UserError, 'daily AI cost'):
                self.issue.action_ai_reanalyse()
        self.assertEqual(client.requests, [])
        self.assertEqual(self.issue.ai_state, 'pending', 'stays pending for tomorrow')
        # calls per day
        self.param.set_param('perf_monitor.ai_daily_cost_limit', '100')
        self._log()
        self.assertIn('AI calls per day (3)', self.Issue._ai_budget()[0])
        # month: calls of earlier days of this month count, not the ones of last month
        self.param.set_param('perf_monitor.ai_daily_limit', '100')
        self.param.set_param('perf_monitor.ai_monthly_cost_limit', '5')
        self.assertIn('monthly AI cost', self.Issue._ai_budget()[0])
        month_start = self.env['perf.ai.call']._day_start().replace(day=1)
        self.env.cr.execute('UPDATE perf_ai_call SET create_date = %s', (month_start - timedelta(days=1),))
        self.env['perf.ai.call'].invalidate_model()
        self.assertEqual(self.Issue._ai_budget()[0], '')

    def test_ai_input_retention(self):
        old = self._log(cost_usd=0.5, input_tokens=1000, output_tokens=100, input_system='sys', input_prompt='prompt',
                        rounds_log='=== round 1', issue_id=self.issue.id)
        recent = self._log(cost_usd=0.25, input_system='sys2', input_prompt='prompt2', rounds_log='r2',
                           issue_id=self.issue.id)
        self.env.flush_all()
        self.env.cr.execute('UPDATE perf_ai_call SET create_date = %s WHERE id = %s',
                            (fields.Datetime.now() - timedelta(days=31), old.id))
        self.env['perf.ai.call'].invalidate_model()
        self.assertEqual(self.param.get_param('perf_monitor.ai_input_retention_days'), '30')
        # the existing daily purge cron blanks the input of calls older than 30 days, keeps tokens / cost
        self.env['perf.agent.slow.call.remote']._cron_purge()
        self.assertEqual((old.input_system, old.input_prompt, old.rounds_log), (False, False, False))
        self.assertEqual((old.input_tokens, old.output_tokens, old.cost_usd, old.status), (1000, 100, 0.5, 'ok'))
        self.assertIn('older than the retention', str(old.input_html))
        self.assertEqual((recent.input_system, recent.input_prompt, recent.rounds_log), ('sys2', 'prompt2', 'r2'))
        self.assertAlmostEqual(self.issue.ai_cost_usd, 0.75, msg='issue totals unchanged')
        # 0 = keep forever; explicit days
        self.param.set_param('perf_monitor.ai_input_retention_days', '0')
        self.assertEqual(self.env['perf.ai.call']._purge_inputs(), 0)
        self.assertEqual(self.env['perf.ai.call']._purge_inputs(days=1), 0, 'recent call kept')

    def _calls(self):
        return self.env['perf.ai.call'].search_count([])

    def test_cost_left_stops_a_run(self):
        # the first call alone could pass what is left: stopped BEFORE the log row, the calls counter is untouched
        self.param.set_param('perf_monitor.ai_daily_cost_limit', '0.01')
        self._log(cost_usd=0.009)
        calls, today = self._calls(), self.env['perf.ai.call']._calls_today()
        script = [tool_use([('list_module', {'name': 'perf_monitor'})], tin=3000, tout=100)]
        with fake_bedrock(self.env, script) as client:
            self.assertFalse(self.issue._ai_analyse('auto'))
        self.assertEqual(client.requests, [])
        self.assertEqual(self.issue.ai_state, 'pending', 'budget stop: no verdict, the cron retries it')
        self.assertIn('cost limit', self.issue.ai_error)
        self.assertEqual((self._calls(), self.env['perf.ai.call']._calls_today()), (calls, today),
                         'no zero-round perf.ai.call row')
        # the cron stops at the first budget stop and logs nothing either
        with fake_bedrock(self.env, script) as client:
            self.assertEqual(self.Issue._ai_analyse_pending(), 1)
        self.assertEqual((client.requests, self._calls()), ([], calls))
        # safety net: a run stopped by cost_left before its first call deletes its row
        with patch.object(perf_ai, 'first_call_cost', lambda *args: 0.0), fake_bedrock(self.env, script) as client:
            self.assertFalse(self.issue._ai_analyse('manual'))
        self.assertEqual((client.requests, self._calls(), self.issue.ai_state), ([], calls, 'pending'))

    def test_cost_left_stops_a_run_after_a_round(self):
        # enough for the first call, not for the second: one row with the usage of that round
        limits = self.issue._ai_limits()
        system = self.issue._ai_system_prompt(limits)
        messages = [{'role': 'user', 'content': [{'text': self.issue._ai_prompt(limits)}]}]
        first = perf_ai.first_call_cost(system, messages, limits, 1.0, 5.0)
        self.param.set_param('perf_monitor.ai_daily_cost_limit', repr(first + 0.002))
        script = [tool_use([('list_module', {'name': 'perf_monitor'})], tin=3000, tout=100)]
        with fake_bedrock(self.env, script) as client:
            self.assertFalse(self.issue._ai_analyse('auto'))
        self.assertEqual(len(client.requests), 1)
        log = self.issue.ai_call_ids
        self.assertEqual((log.status, log.rounds, log.input_tokens), ('limit', 1, 3000))
        self.assertIn('cost limit', log.error)
        self.assertEqual(self.issue.ai_state, 'pending')

    def test_budget_stop_is_a_flag_not_the_text(self):
        # a translated budget message still keeps the issue pending, a run stop mentioning the words does not
        self.issue._ai_fail('limit', 'Limite atteinte : appels IA par jour (3)', budget=True)
        self.assertEqual(self.issue.ai_state, 'pending')
        self.issue._ai_fail('limit', 'limit reached: AI calls per day / daily AI cost / cost limit')
        self.assertEqual(self.issue.ai_state, 'limit')
        self.issue.ai_state = 'pending'
        self.param.set_param('perf_monitor.ai_daily_limit', '1')
        self._log()
        reason, left, kind = self.Issue._ai_budget()
        self.assertEqual((left, kind), (0.0, 'calls_per_day'))
        self.assertIn(kind, perf_ai.BUDGET_KINDS)
        with patch.object(type(self.Issue), '_ai_budget', lambda issue, limits=None: ('Limite', 0.0, 'daily_cost')):
            self.assertFalse(self.issue._ai_analyse('manual'))
        self.assertEqual((self.issue.ai_state, self.issue.ai_error), ('pending', 'Limite'))

    def test_failed_rerun_keeps_the_done_analysis(self):
        with fake_bedrock(self.env, [answer(ANSWER)]):
            self.assertTrue(self.issue._ai_analyse('auto'))
        self.assertEqual(self.issue.ai_state, 'done')
        done_date = self.issue.ai_date
        # run stop (rounds) on a manual re-run: the earlier analysis stays visible
        self.param.set_param('perf_monitor.ai_max_tool_rounds', '1')
        with fake_bedrock(self.env, [tool_use([('list_module', {'name': 'perf_monitor'})], text='half')]):
            self.assertFalse(self.issue._ai_analyse('manual'))
        self.assertEqual((self.issue.ai_state, self.issue.ai_root_cause, self.issue.ai_date),
                         ('done', 'Generic write override on base.', done_date))
        self.assertIn('max model calls', self.issue.ai_error)
        self.assertEqual(self.issue.ai_partial, 'half')
        self.assertIn('Root cause (AI): Generic write override on base.', self.issue._claude_code_prompt())
        # error on a re-run: same
        with fake_bedrock(self.env, [RuntimeError('ThrottlingException')]):
            self.issue._ai_analyse('manual')
        self.assertEqual(self.issue.ai_state, 'done')
        self.assertIn('ThrottlingException', self.issue.ai_error)
        # budget stop on a re-run: same
        self.param.set_param('perf_monitor.ai_daily_limit', '1')
        self.assertFalse(self.issue._ai_analyse('manual'))
        self.assertEqual(self.issue.ai_state, 'done')
        self.assertIn('AI calls per day', self.issue.ai_error)
        # a successful re-run clears the error
        self.param.set_param('perf_monitor.ai_daily_limit', '100')
        with fake_bedrock(self.env, [answer(ANSWER)]):
            self.assertTrue(self.issue._ai_analyse('manual'))
        self.assertFalse(self.issue.ai_error)
        self.assertFalse(self.issue.ai_partial)

    def test_budget_stop_keeps_new_issue_pending(self):
        self.param.set_param('perf_monitor.ai_daily_limit', '1')
        self._log()
        # race: the button passed the check, the budget is gone when the analysis starts
        self.assertFalse(self.issue._ai_analyse('manual'))
        self.assertEqual(self.issue.ai_state, 'pending')
        # a run stop (not a budget stop) still gives "limit"
        self.param.set_param('perf_monitor.ai_daily_limit', '100')
        self.param.set_param('perf_monitor.ai_max_tool_rounds', '1')
        with fake_bedrock(self.env, [tool_use([('list_module', {'name': 'perf_monitor'})])]):
            self.issue._ai_analyse('auto')
        self.assertEqual(self.issue.ai_state, 'limit')

    def test_cost_log_is_written_before_and_during_the_run(self):
        seen = []

        def script(request, n):
            self.env.cr.execute('SELECT status, input_tokens, rounds FROM perf_ai_call WHERE issue_id = %s',
                                (self.issue.id,))
            seen.append(self.env.cr.fetchall())
            if n == 1:
                return tool_use([('list_module', {'name': 'perf_monitor'})], tin=1500)
            return answer(ANSWER, tin=2500)
        with fake_bedrock(self.env, script):
            self.assertTrue(self.issue._ai_analyse('auto'))
        self.assertEqual(seen, [[('running', 0, 0)], [('running', 1500, 1)]],
                         'the row exists before the first call and holds the usage of every finished round')
        log = self.issue.ai_call_ids
        self.assertEqual((log.status, log.input_tokens, log.rounds), ('ok', 4000, 2))
        # committed (outside tests) at creation, after every round and after the final status
        commits = []

        def _commit(issue):
            issue.env.cr.execute('SELECT status, rounds FROM perf_ai_call WHERE issue_id = %s ORDER BY id DESC '
                                 'LIMIT 1', (issue.id,))
            commits.append(issue.env.cr.fetchone())
        with patch.object(type(self.issue), '_ai_commit', _commit), fake_bedrock(self.env, script):
            self.assertTrue(self.issue._ai_analyse('manual'))
        self.assertEqual(commits, [('running', 0), ('running', 1), ('running', 2), ('ok', 2)],
                         "the final status is committed: 'running' only stays when the worker was killed")

    def test_time_caps(self):
        issue = self.issue
        limits = issue._ai_limits()
        with patch.dict('odoo.tools.config.options', {'workers': 2, 'limit_time_real': 0, 'limit_time_real_cron': -1}):
            capped, timing = issue._ai_time_caps('manual', limits)
            self.assertEqual((capped['ai_max_tool_rounds'], timing['read_timeout'], timing['deadline']),
                             (8, perf_ai.DEFAULT_READ_TIMEOUT, None))
        with patch.dict('odoo.tools.config.options', {'workers': 2, 'limit_time_real': 120, 'limit_time_real_cron': 0}):
            capped, timing = issue._ai_time_caps('manual', limits)
            # 0.8 x 120 = 96 s, 90 % for the calls: 20 s per call (minimum), 4 calls: rounds x read_timeout <= 96
            self.assertEqual((capped['ai_max_tool_rounds'], timing['read_timeout'], timing['retries']), (4, 20, 1))
            self.assertLessEqual(capped['ai_max_tool_rounds'] * timing['read_timeout'], 0.8 * 120)
            # one round: the first call is never refused by the deadline
            one, timing = issue._ai_time_caps('manual', dict(limits, ai_max_tool_rounds=1))
            self.assertEqual((one['ai_max_tool_rounds'], timing['read_timeout']), (1, 86))
            self.assertEqual(issue._ai_time_limit('auto'), 0, 'limit_time_real_cron = 0: no limit for the cron')
        with patch.dict('odoo.tools.config.options', {'workers': 2, 'limit_time_real': 120, 'limit_time_real_cron': 1000}):
            capped, timing = issue._ai_time_caps('auto', limits)
            self.assertEqual((capped['ai_max_tool_rounds'], timing['read_timeout']), (8, 90))
            self.assertLessEqual(8 * 90, 0.8 * 1000)
        # threaded server (workers = 0): Odoo never kills the request / cron thread, no cap, no warning
        with patch.dict('odoo.tools.config.options', {'workers': 0, 'limit_time_real': 30, 'limit_time_real_cron': 30}):
            self.assertEqual((issue._ai_time_limit('manual'), issue._ai_time_limit('auto')), (0, 0))
            capped, timing = issue._ai_time_caps('manual', limits)
            self.assertEqual((capped['ai_max_tool_rounds'], timing['read_timeout'], timing['deadline']),
                             (8, perf_ai.DEFAULT_READ_TIMEOUT, None))
            with self.assertNoLogs('odoo.addons.perf_monitor.models.perf_ai', 'WARNING'), \
                    fake_bedrock(self.env, [answer(ANSWER)]):
                self.assertTrue(issue._ai_analyse('manual'))
        issue.ai_state = 'pending'   # a new issue again: a stopped run gives 'limit' below
        with patch.dict('odoo.tools.config.options', {'workers': 2, 'limit_time_real': 30}):
            with fake_bedrock(self.env, [tool_use([('list_module', {'name': 'perf_monitor'})]), answer(ANSWER)]) \
                    as client, self.assertLogs('odoo.addons.perf_monitor.models.perf_ai', 'WARNING') as logs:
                issue._ai_analyse('manual')
            self.assertIn('Bedrock read timeout 20 s (1 model calls max) because of limit_time_real = 30', logs.output[0])
            self.assertEqual(client.client_kwargs, {'read_timeout': 20, 'retries': 1})
            self.assertIn('at most 1 model calls', client.requests[0]['system'][0]['text'])
            self.assertEqual((len(client.requests), issue.ai_state), (1, 'limit'))
            self.assertIn('max model calls / tool rounds (1)', issue.ai_error)

    def test_negative_limits_and_settings_constraints(self):
        self.param.set_param('perf_monitor.ai_max_files_read', '-3')
        self.param.set_param('perf_monitor.ai_daily_limit', '-1')
        self.param.set_param('perf_monitor.ai_daily_cost_limit', '-2.5')
        limits = self.Issue._ai_limits()
        self.assertEqual((limits['ai_max_files_read'], limits['ai_daily_limit'], limits['ai_daily_cost_limit']),
                         (20, perf_ai.DEFAULT_DAILY_LIMIT, 5.0), 'negative -> default')
        for fname in ('perf_ai_max_files_read', 'perf_ai_daily_limit', 'perf_ai_monthly_cost_limit',
                      'perf_ai_chat_max_turns'):
            with self.assertRaisesRegex(ValidationError, 'cannot be negative'):
                self.env['res.config.settings'].create({fname: -5})
        # negative values stored before the rule (the old form accepted them): the Settings still save
        with self.assertLogs('odoo.addons.perf_monitor.models.res_config_settings', 'WARNING') as logs:
            settings = self.env['res.config.settings'].create({'perf_ai_chat_max_turns': 4})
        self.assertIn('perf_monitor.ai_daily_limit is negative (-1)', '\n'.join(logs.output))
        self.assertEqual((settings.perf_ai_daily_limit, settings.perf_ai_daily_cost_limit), (-1, -2.5))
        settings.execute()
        self.assertEqual(self.param.get_param('perf_monitor.ai_chat_max_turns'), '4')
        with self.assertRaisesRegex(ValidationError, 'AI calls per day cannot be negative'):
            self.env['res.config.settings'].create({'perf_ai_daily_limit': -2})   # changed: refused
        for key in ('ai_max_files_read', 'ai_daily_limit', 'ai_daily_cost_limit'):
            self.param.set_param('perf_monitor.%s' % key, False)
        self.env['res.config.settings'].create({'perf_ai_max_files_read': 0})

    def test_access_log_rows_cannot_be_deleted(self):
        manager = self.env['res.users'].create({
            'name': 'AI log manager', 'login': 'perf_ai_log_manager',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('perf_monitor.group_perf_manager').id])]})
        for model in ('perf.ai.call', 'perf.issue.chat.message'):
            self.assertTrue(self.env[model].with_user(manager).check_access_rights('read', raise_exception=False))
            self.assertFalse(self.env[model].with_user(manager).check_access_rights('unlink', raise_exception=False),
                             model)

    def test_ai_model_per_issue_and_temperature(self):
        Model = self.env['perf.ai.model']
        haiku = self.env.ref('perf_monitor.ai_model_haiku_4_5')
        opus = self.env.ref('perf_monitor.ai_model_opus_5_5')
        # default: the legacy perf_monitor.ai_model_id matches the seeded Haiku row
        self.assertEqual(Model._default_model(), haiku)
        # not stored at create, no Python default: empty means the Settings default at run time
        self.assertFalse(self.issue.ai_model_id, 'create() leaves the AI model empty')
        self.assertFalse(self.env['perf.issue']._fields['ai_model_id'].default)
        self.assertEqual(self.issue._ai_conf()['record'], haiku, 'empty AI model -> Settings default')
        # a later change of the Settings default applies to the issues without their own model
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('perf_monitor.ai_default_model_id', str(opus.id))
        self.assertEqual(self.issue._ai_conf()['record'], opus)
        ICP.set_param('perf_monitor.ai_default_model_id', str(haiku.id))
        self.assertEqual(self.issue._ai_conf()['record'], haiku)
        with fake_bedrock(self.env, [answer(ANSWER, tin=10000, tout=1000)]) as client:
            self.assertTrue(self.issue._ai_analyse('manual'))
        self.assertEqual(client.requests[0]['inferenceConfig']['temperature'], 0.2)
        # the issue's model: id, region, prices (cost log), no temperature for Opus 5.5
        opus.region = 'us-east-1'
        self.issue.ai_model_id = opus
        with fake_bedrock(self.env, [answer(ANSWER, tin=10000, tout=1000)]) as client:
            self.issue.action_ai_reanalyse()
        request = client.requests[0]
        self.assertEqual((request['modelId'], client.regions[0]), ('eu.anthropic.claude-opus-5-5', 'us-east-1'))
        self.assertNotIn('temperature', request['inferenceConfig'])
        self.assertEqual((self.issue.ai_state, self.issue.ai_model), ('done', 'eu.anthropic.claude-opus-5-5'))
        log = self.issue.ai_call_ids.sorted('id')[-1]
        self.assertEqual((log.trigger, log.ai_model_id, log.price_input_mtok, log.price_output_mtok),
                         ('manual', opus, 4.0, 20.0))
        self.assertAlmostEqual(log.cost_usd, (10000 * 4.0 + 1000 * 20.0) / 1e6)
        # per-issue sums of the calls (analysis + chat) and the last model
        self.assertEqual((self.issue.ai_tokens_in, self.issue.ai_tokens_out), (20000, 2000))
        self.assertAlmostEqual(self.issue.ai_cost_usd, (10000 * 1.0 + 1000 * 5.0) / 1e6 + log.cost_usd)
        self.assertEqual(self.issue.ai_last_model, 'eu.anthropic.claude-opus-5-5')
        # history: every analysis run is a chatter note (model, tokens, cost, result), never an e-mail
        notes = self.issue.message_ids.filtered(lambda m: 'AI analysis (Re-analyse)' in (m.body or ''))
        self.assertEqual(len(notes), 2)
        self.assertIn('eu.anthropic.claude-opus-5-5', notes[0].body)
        self.assertIn('10000 input / 1000 output tokens', notes[0].body)
        self.assertFalse(notes.filtered(lambda m: m.message_type == 'email'))
        # a model that refuses the temperature: ValidationException -> retried ONCE without it
        haiku.send_temperature = True
        self.issue.ai_model_id = haiku

        def script(request, n):
            if 'temperature' in request['inferenceConfig']:
                return RuntimeError('An error occurred (ValidationException) when calling the Converse operation: '
                                    'temperature is deprecated for this model.')
            return answer(ANSWER)
        with fake_bedrock(self.env, script) as client, \
                self.assertLogs('odoo.addons.perf_monitor.models.perf_ai', 'WARNING') as logs:
            self.assertTrue(self.issue._ai_analyse('manual'))
        self.assertEqual(['temperature' in r['inferenceConfig'] for r in client.requests], [True, False])
        self.assertIn('refused the temperature parameter', logs.output[0])
        log = self.issue.ai_call_ids.sorted('id')[-1]
        self.assertEqual((log.status, log.rounds), ('ok', 1))
        self.assertIn('refused the temperature parameter', log.rounds_log)
        # other ValidationExceptions are errors, not retried
        with fake_bedrock(self.env, [RuntimeError('ValidationException: maxTokens too large')]) as client:
            self.assertFalse(self.issue._ai_analyse('manual'))
        self.assertEqual(len(client.requests), 1)
        # archived issue model -> Settings default; no usable row -> the legacy parameters
        self.issue.ai_model_id = opus
        opus.active = False
        self.assertEqual(self.issue._ai_conf()['record'], haiku)
        self.param.set_param('perf_monitor.ai_default_model_id', str(opus.id))
        self.assertEqual(Model._default_model(), haiku, 'archived default -> legacy id match')
        Model.search([]).write({'active': False})
        conf = self.issue._ai_conf()
        self.assertEqual((conf['record'], conf['model_id'], conf['price_in'], conf['send_temperature']),
                         (Model, 'eu.anthropic.claude-haiku-4-5-20251001-v1:0', 1.0, True))
        # Settings: "Default AI model" stored as the record id
        haiku.active = True
        settings = self.env['res.config.settings'].create({'perf_ai_default_model_id': haiku.id})
        settings.execute()
        self.assertEqual(self.param.get_param('perf_monitor.ai_default_model_id'), str(haiku.id))
        self.assertFalse(hasattr(type(self.issue), 'action_ai_deep_analyse'), 'no Deep analyse any more')

    def test_input_sent_is_stored_redacted(self):
        self.issue.top_sql = "SELECT id FROM res_partner WHERE name = 'Secret Customer' AND ref = 4711"
        script = [tool_use([('read_file', {'path': 'perf_monitor/tests/fixtures/sample_base_override.py'})],
                           text='Reading the override'), answer(ANSWER)]
        with fake_bedrock(self.env, script) as client:
            self.assertTrue(self.issue._ai_analyse('auto'))
        log = self.issue.ai_call_ids
        first = client.requests[0]
        # exactly what was sent: the same strings as the live request (same redaction: strip_sql)
        self.assertEqual(log.input_system, first['system'][0]['text'])
        self.assertEqual(log.input_prompt, first['messages'][0]['content'][0]['text'])
        self.assertEqual(log.input_prompt, self.issue._ai_prompt(self.issue._ai_limits()))
        for text in (log.input_system, log.input_prompt, log.rounds_log):
            self.assertNotIn('Secret Customer', text)
            self.assertNotIn('4711', text)
        self.assertIn('res_partner', log.input_prompt)
        self.assertIn('tool call read_file', log.rounds_log)
        self.assertIn('Reading the override', log.rounds_log)
        self.assertIn("_inherit = 'base'", log.rounds_log, 'tool result stored')
        self.assertGreater(log.estimated_input_tokens, 0)
        self.assertEqual(log.estimate_method, 'chars/4', 'perf_monitor.ai_tiktoken is off by default')
        html = str(self.issue.ai_input_html)
        self.assertIn('System prompt', html)
        self.assertIn('characters, ~', html)
        self.assertNotIn('Secret Customer', html)

    def test_unparseable_answer_is_kept(self):
        with fake_bedrock(self.env, [answer('Sorry, here is my idea {not json} without the object.')]):
            self.assertFalse(self.issue._ai_analyse('auto'))
        self.assertEqual(self.issue.ai_state, 'error')
        self.assertIn('not one valid JSON object', self.issue.ai_error)
        self.assertIn('Raw answer', self.issue.ai_error)
        self.assertEqual(self.issue.ai_partial, 'Sorry, here is my idea {not json} without the object.')
        self.assertEqual(self.issue.ai_call_ids.status, 'error')
        # prose around the object: parsed
        with fake_bedrock(self.env, [answer('Here it is:\n%s\nHope it helps {really}.' % ANSWER)]):
            self.assertTrue(self.issue._ai_analyse('manual'))
        self.assertEqual(self.issue.ai_state, 'done')

    def test_tool_use_setting(self):
        settings = self.env['res.config.settings'].create({})
        self.assertTrue(settings.perf_ai_tool_use_enabled, 'on by default')
        self.assertTrue(settings.perf_ai_chat_enabled)
        self.assertTrue(settings.perf_ai_chat_tools_enabled)
        settings.perf_ai_tool_use_enabled = False
        settings.perf_ai_max_tool_rounds = 3
        settings.execute()
        self.assertEqual(self.param.get_param('perf_monitor.ai_tool_use_enabled'), 'False')
        limits = self.Issue._ai_limits()
        self.assertEqual((limits['ai_tool_use_enabled'], limits['ai_max_tool_rounds']), (False, 3))
        self.assertEqual(limits['ai_monthly_cost_limit'], 50.0)
        with fake_bedrock(self.env, [answer(ANSWER)]) as client:
            self.issue._ai_analyse('manual')
        self.assertNotIn('toolConfig', client.requests[0])
        self.assertEqual(self.issue.ai_state, 'done')
