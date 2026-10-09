# Changelog (perf_monitor)

## 16.0.1.12.0

### AI models
- New **AI models** table (`perf.ai.model`, Configuration > AI models, managers only): Bedrock id / inference
  profile, region, USD prices per 1M input / output tokens, *Send temperature*, active, sequence. Seeded:
  Haiku 4.5 and Opus 5.5 (noupdate).
- Settings: **Default AI model** (stored as the record id in `perf_monitor.ai_default_model_id`); the old free-text
  model id / prices stay as fallback. Migration links existing installs to their old model and prices.
- Issue field **AI model** in the *AI advice* tab (not in the form header); Re-analyse and the chat use its id,
  region and prices. The AI call log stores the model record and the prices used. No Python default on the field
  (an upgrade would evaluate it before the `perf_ai_model` table exists) and nothing is stored at create: an
  empty value means the Settings default at run time (form placeholder *Settings default*), so a later change of
  the default applies to every issue without its own model.
- Fix: Claude Opus 5.5 refused every call (`ValidationException: temperature is deprecated for this model`): the
  temperature is only sent when the model row allows it, and a call refused for that reason is retried once
  without it (logged).
- Removed the *Deep analyse* button and its settings (the model dropdown covers it); the old `ai_deep_*`
  parameters are left untouched; an old deep model id becomes an AI model row.

### Tokens, cost, input
- Issue: stored **AI input / output tokens, AI cost, last AI model** (all AI calls of the issue, analysis + chat),
  shown in the AI tab; the *AI calls* tab (now visible to every Perf user) shows model, tokens, cost, trigger,
  status and error, and opens a call form.
- Every AI call stores exactly what was sent: system prompt, prompt / conversation, and per round the model answer,
  tool calls and tool results (each clipped to 20,000 characters), plus an estimated prompt size. New tab
  **Input sent to AI** (latest analysis; other calls from the *AI calls* table).
- Optional `tiktoken` for the estimate (not in `external_dependencies`; fallback characters / 4), only when the
  system parameter `perf_monitor.ai_tiktoken` is on (default off) and only from an AI run: a form never loads it
  (its first use may download the encoding file without a timeout).
- Retention: the daily *Purge old agent slow calls* cron blanks the stored input / tool rounds of AI calls older
  than `perf_monitor.ai_input_retention_days` (default 30, 0 = keep); tokens / cost rows stay.
- The *Input sent to AI* / *AI calls* tabs are readable by every Perf viewer (owner decision, see README),
  including the tool results with addon source code and the AI error text.
- `ai_max_input_tokens` default 60000 -> 150000 (code, data, Settings); the chat token default is unchanged.

### Answers and prompts
- Robust answer parser: code fences stripped, every `{` tried with `JSONDecoder.raw_decode`, the first object with
  the expected keys wins, text before / after is ignored. An answer without a valid object is kept as **Raw
  answer** in the AI tab (state Error with a clear message), never dropped. Stricter system prompt (one JSON
  object, no text around it, no code fences).
- Claude Code prompt tab: the full prompt, read-only, in a scrollable monospace block with its size (characters,
  estimated tokens); Copy button kept.

### Chat
- Chat pane in the issue form: bubbles (questions right, AI left), markdown-ish answers with copyable code blocks
  (diff lines coloured), per-answer model / tokens / cost, **New chat** (sessions, `perf.issue.chat.session`),
  **Previous chats**, chat **model selector**, **Use previous conversation as context** switch.
- New chat guard: blocks only empty, > 2000 characters, secret / prompt-extraction / injection phrases, and clear
  off-topic requests when the chat has no answered turn and the question names nothing of the issue. Plain words
  such as token / shell / ssh / secret in code questions pass; short follow-ups pass. Refusals are logged with
  their reason (English + Roman Urdu notice) and the question is put back in the box. Secret nouns must be the
  object of the request ("show me the access token validation code" passes), "developer mode" / "odoo.conf" are
  refused only in jailbreak / ask-for-the-file phrasing, fewer off-topic false positives (stock price, crypto,
  story point, BoM recipe).
- Chat system prompt: connect short follow-ups to the conversation; refuse unrelated questions in one sentence.
- Chat tab redesigned like a modern chat app: full-height panel, slim top bar (New chat pill, Previous chats, model,
  History switch, usage, one-line disclaimer), centred message column (questions = right bubbles, answers = avatar +
  model name, no border; notices = centred pills), empty state with example questions, one rounded composer with
  a round Send button. Styles in `static/src/scss/perf_chat.scss` (`o_perf_chat_*`); `static/src/js/perf_chat.js`
  adds Enter to send / Shift+Enter new line, auto-grow, scroll to the last message, clickable examples (the plain
  buttons still work without it). Message text stays HTML-escaped.
- Greetings / thanks alone ("hi", "salam", "assalamualaikum", "shukriya", "ok" ...) get a canned answer without a
  model call (no cost, not an answered turn); "hi, why is write slow" still goes to the model.

### Issues
- Chatter history (notes only, never an e-mail): issue created, state / assignee changes (tracking), each AI analysis
  run (model, tokens, cost, result); recurring occurrences are not logged.
- List: *Created* (sortable) and *Age (days)* columns.
- Call site: frames of `wags_logger/` (cursor patch), `odoo/sql_db.py`, `odoo/models.py` and the monitors are skipped;
  migration recomputes the call site of existing issues from their stored slow calls when possible. The first
  application frame is picked before the stored call site is clipped to 256 characters.

### Servers: pg_stat_statements status
- `perf.server` **Top SQL (pg_stat_statements)** (`pgss_status`: unknown / ok / missing / unreadable) and
  `pgss_checked_at`, set from the payload key `pgss` (perf_monitor_agent >= 16.0.1.4.0, perf_collector.py >= 1.0.2;
  older senders: unknown, still ingested). Badge on the server kanban card and list, warning with the fix on the
  form, filter "Top SQL OFF", hint on the Top SQL empty state. README: how to enable it (AWS RDS / postgresql.conf)
  and its risks; nothing is installed automatically.
- `perf_collector.py` 1.0.2: pg_stat_statements missing / unreadable is logged once per change (INFO) instead of a
  warning every cycle, and reported as `pgss`.
