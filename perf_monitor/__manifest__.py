# -*- coding: utf-8 -*-
{
    'name': 'Performance Monitor',
    'version': '16.0.1.12.0',
    'category': 'Productivity/Performance',
    'summary': 'Right-sizing, top SQL and live query monitoring pushed by a light collector agent',
    'description': """
Performance Monitor (Phase 1)
=============================
* Per-server OS metrics (CPU / RAM / disk / net / DB latency) pushed by a light collector.
* Right-sizing stub (needs 14 days of data).
* Top SQL deltas from pg_stat_statements and live long / blocked / idle-in-transaction queries. Each server
  shows "Top SQL: ON" / "Top SQL OFF - pg_stat_statements missing" (reported by the agent / collector; the
  extension is never installed automatically, see README.md).
* Slow calls pushed by perf_monitor_agent (companion module of the monitored DBs), per source DB, with
  their dashboard, dashboard tile (one row per tile of a slow multi-tile call), menu path and user.
* Performance issues (tasks) built by rules from the slow calls: one per SQL fingerprint / model.method /
  dashboard tile, DB tags, severity rules (duration, frequency, SQL count, N+1, seq scans), never
  auto-closed, reopened when a closed issue happens again. Assigned to the Settings "Issue assignee"
  (default: Yasir, else Administrator) with an Odoo To-Do activity (never an e-mail); menu My Issues.
* Only actionable slowness makes an issue (SQL share / slow query, N+1, recurring in 7 days); skip rules
  (Skip button, 7 / 30 days / forever) keep a known slowness out of the issues; plain-language
  explanation (English + Roman Urdu) on every issue.
* AI analysis of new issues through Amazon Bedrock (opt-in; boto3, IAM role; English + Roman Urdu; advice
  only, nothing applied): the model may read this server's addons through sandboxed read-only tools
  (read_file / grep / list_module / find_symbol); limits per run / day / month (tool rounds, tokens, files,
  USD), stopped runs keep their partial answer ("limit reached"); every run logged (tokens, cost, rounds,
  files).
* AI models table (Configuration > AI models: Bedrock id, region, USD prices, "Send temperature"; seeded with
  Haiku 4.5 and Opus 5.5): Settings "Default AI model", per-issue "AI model" in the AI advice tab (also used by the
  chat, which has its own selector); a model that refuses the temperature parameter is retried once without it.
  Per-issue AI tokens / cost / last model, "Input sent to AI" (exact system prompt, prompt and tool rounds of
  every call), tolerant answer parser (raw answer kept when it is not valid JSON). Optional tiktoken (not
  required; system parameter perf_monitor.ai_tiktoken, default off) for token estimates.
* AI time limits: the model calls run inside the Odoo worker (HTTP request for Re-analyse /
  chat, cron job for new issues). A run stops before it could pass 80 % of limit_time_real
  (limit_time_real_cron for the cron): read_timeout x model calls <= 0.8 x limit; the cost log row is
  written before the first model call and updated (committed) after every round, so a killed worker never
  hides spent tokens. With the Odoo default limit_time_real = 120 s a run gets at most 4 model calls of
  20 s (a warning is logged when a read timeout is below 60 s); set limit_time_real >= 600 (and
  limit_time_real_cron >= 900) on the monitoring server for tool rounds with a large model (Opus). No cap on
  a threaded server (workers = 0): Odoo only logs the limit there, it never kills the request.
* Per-issue AI chat pane (managers; chat-app layout: bubbles, code blocks, per-answer model / tokens / cost, one
  composer with Enter to send, New chat / Previous chats, model selector, "Use previous conversation" switch;
  greetings get a canned answer, secret / prompt requests and clear off-topic questions without context are
  refused, both without a model call, a refused question stays in the box; own "Chat tools" switch),
  issue fields from the monitored databases framed as untrusted data in every prompt, "Claude Code prompt" tab
  (copy button) with the redacted evidence and instructions, lessons library fed by "AI solution was wrong".
* Durations shown in seconds on issues and slow calls (the *_ms values stay stored). Issue history in the chatter
  (created, state / assignee changes, AI runs; notes only, never an e-mail), "Age (days)" column.
* The real Python call site: wags_logger's cursor patch and the SQL layer frames are never used as call site.
* Right-size card from CloudWatch (EC2 + RDS, 14-day p95 CPU / RAM / connections, on-demand prices).
* "Add Server" wizard issuing a per-server token (copy button) with the perf_monitor_agent settings to paste
  (preferred: module only, the agent pushes slow calls AND metrics / Top SQL / live queries with that token);
  the one-line root collector installer stays available as an optional / legacy alternative.
* Server cards are identified by their unique Name (the database name may repeat across hosts).
""",
    'author': "wags.sa",
    'license': 'LGPL-3',
    'depends': ['base', 'mail', 'web'],
    # optional Python packages (NOT in external_dependencies: the module installs without them): boto3 (AI layer),
    # tiktoken (token estimates of the prompts; falls back to characters / 4). See README.md.
    'data': [
        'security/perf_monitor_security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'data/ir_cron.xml',
        'data/perf_ai_model_data.xml',
        'wizard/perf_server_add_wizard_views.xml',
        'wizard/perf_issue_skip_wizard_views.xml',
        'wizard/perf_ai_lesson_wizard_views.xml',
        'views/perf_metric_views.xml',
        'views/perf_sql_snapshot_views.xml',
        'views/perf_live_query_views.xml',
        'views/perf_server_views.xml',
        'views/perf_agent_slow_call_remote_views.xml',
        'views/perf_issue_views.xml',
        'views/perf_skip_rule_views.xml',
        'views/perf_ai_lesson_views.xml',
        'views/perf_ai_model_views.xml',
        'data/perf_ai_lesson_data.xml',
        'views/res_config_settings_views.xml',
        'views/perf_monitor_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'perf_monitor/static/src/scss/perf_chat.scss',
            'perf_monitor/static/src/js/perf_chat.js',
        ],
    },
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': True,
}
