# -*- coding: utf-8 -*-
"""Pure text helpers of the AI layer (no Odoo import: unit-tested without a database).

- parse_ai_output: the JSON object of an analysis answer, tolerant to prose / fences / several objects;
- chat_guard: cheap pre-filter of the chat questions (no model call when refused);
- chat_smalltalk: canned reply to a bare greeting / thanks (no model call, no cost);
- real_callsite: the first application frame of a call site (skips wags_logger's cursor patch, the SQL layer);
- estimate_text_tokens: token estimate shown in the UI (characters / 4; tiktoken only when asked for, i.e.
  perf_monitor.ai_tiktoken is on, and never from a form: loading it may download its encoding file);
- render_chat_html / pre_block: HTML of the chat bubbles and of the "input sent" blocks (escaped text)."""
import html
import json
import re

OUTPUT_KEYS = ('explanation_en', 'explanation_roman_urdu', 'root_cause', 'fix', 'index_sql',
               'index_justification', 'expected_impact', 'risk')
ROUND_ITEM_CHARS = 20000          # one tool call / tool result / text block in the stored rounds log


# ---------------------------------------------------------------------------- analysis answer
_RX_FENCE_LINE = re.compile(r'^[ \t]*```[\w+-]*[ \t]*$', re.M)


def _json_objects(text):
    """Every JSON object that decodes at a '{' of ``text`` (in order; nested ones too)."""
    decoder = json.JSONDecoder()
    pos = text.find('{')
    while pos >= 0:
        try:
            obj = decoder.raw_decode(text, pos)[0]
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            yield obj
        pos = text.find('{', pos + 1)


def parse_ai_output(text):
    """The analysis object of the model answer. Tolerates ```json fences, prose before / after the object,
    other braces and several objects: the first object with the expected keys (both explanations) wins,
    else the first object with any expected key. Raises ValueError (clear message) when none is usable."""
    text = _RX_FENCE_LINE.sub('', (text or '').strip()).strip()
    if '{' not in text:
        raise ValueError('no JSON object in the answer')
    best = None
    for obj in _json_objects(text):
        if obj.get('explanation_en') and obj.get('explanation_roman_urdu'):
            best = obj
            break
        if best is None and any(k in obj for k in OUTPUT_KEYS):
            best = obj
    if best is None:
        raise ValueError('no valid JSON object with the expected keys in the answer')
    out = {}
    for key in OUTPUT_KEYS:
        value = best.get(key, '')
        if isinstance(value, (list, dict)):
            value = json.dumps(value, indent=1, ensure_ascii=False)
        out[key] = str(value or '').strip()
    if not out['explanation_en'] or not out['explanation_roman_urdu']:
        raise ValueError('answer lacks the English / Roman Urdu explanation')
    return out


# ---------------------------------------------------------------------------- chat guard
MAX_QUESTION_CHARS = 2000
_SECRET_NOUN = (r'(?:api[\s_-]?keys?|passw(?:or)?ds?|secret[\s_-]?(?:keys?|access[\s_-]?keys?)|secrets|'
                r'access[\s_-]?keys?|credentials?|bearer[\s_-]?tokens?|auth(?:entication)?[\s_-]?tokens?|'
                r'access[\s_-]?tokens?|private[\s_-]?keys?|ssh[\s_-]?keys?|aws_\w+|db_password|admin_passwd|'
                r'master[\s_-]?password|session[\s_-]?(?:ids?|cookies?))')
_ASK_VERB = (r'(?:reveal|show|print|give|tell|display|dump|leak|share|expose|paste|provide|bata\w*|dikha\w*|'
             r'de\s?do|bhej\w*)')
# only pronouns / determiners / owners between the verb and the secret: "show me the api key" is refused,
# "show me how the api key check is cached" is not
_GAP = (r'(?:\s+(?:me|us|the|a|your|its|our|my|this|that|all|of|current|aws|odoo|admin|db|database|server|'
        r"server's|production|prod|real|actual|full|its|their))*")
# the secret noun must END the request ("... the api key", "... api key?", "... api key of this server"):
# "show me the access token validation code" / "print the db_password usage" are code questions
_SECRET_END = r'(?:\s+(?:of|for)\b|(?:\s+(?:please|pls|plz|now))?\s*[?.!]*\s*$)'
_RX_SECRET_ASK = re.compile(
    # "show me the api key", "give the aws secret access key"
    r'\b%s%s\s+%s%s|'
    # "what is the password?", "what's the AWS access key of this server"
    r"\b(?:what|which|where)(?:'s|\s+is|\s+are|\s+was)%s\s+%s%s|"
    # "what is the secret?" (a bare "secret" only as the whole object: "the secret of a fast read_group" passes)
    r"(?:\b(?:what|which)(?:'s|\s+is)|\b%s)%s\s+secret\s*[?.!]*\s*$|"
    # Roman Urdu: "password kya hai", "api key batao", "access key do"
    r'\b%s\s+(?:kya\s+hai|kia\s+hai|batao|bata\s?do|dikhao|de\s?do|do|chahiye|bhejo)\b'
    % (_ASK_VERB, _GAP, _SECRET_NOUN, _SECRET_END, _GAP, _SECRET_NOUN, _SECRET_END, _ASK_VERB, _GAP,
       _SECRET_NOUN), re.I)
_RX_PROMPT_ASK = re.compile(
    # "system prompt", not the verb: "does the system prompt the user before this write?"
    r'\byour\s+system[\s_-]*prompts?\b|\bsystem[\s_-]*prompt\b(?!\s+(?:the\s+|a\s+|an\s+|any\s+|each\s+|every\s+)?'
    r'(?:users?|operators?|admins?|customers?|employees?|cashiers?|managers?|me|us|him|her|them)\b)|'
    r'\b%s%s\s+(?:your|hidden|initial|original|secret|system)\s+(?:instructions|rules|prompt)\b|'
    # "what are your rules" (not "what are your rules for creating an index?")
    r'\b(?:what|which)\s+(?:are|is)\s+your\s+(?:instructions|rules|prompt)\b(?!\s+(?:for|on|about|of)\b)|'
    r'\b(?:ignore|disregard|forget)\s+(?:(?:all|any|the|of)\s+)*'
    r'(?:previous|above|prior|earlier|preceding|your|these|those|my|all)\s+(?:the\s+)?'
    r'(?:instructions|rules|prompts?|guidelines|directions|messages)\b|'
    # role change ("you are now a shell"), not "so you are now sure the cache is needed?"
    r'\byou\s+are\s+now\s+(?:an?|in|free|unrestricted|unfiltered|dan)\b|'
    # "developer mode" is an everyday Odoo term: only the jailbreak phrasing is refused
    r'\b(?:enter|switch\s+(?:in)?to)\s+developer\s+mode\b|'
    r'^\W*(?:please\s+)?(?:enable|activate|turn\s+on)\s+developer\s+mode\b(?!\s+(?:to|in|for|on)\b)|'
    r'\bjail\s?break\w*|\bDAN\s+mode\b|'
    r'\bpretend\s+(?:to\s+be|you\s+are)\b|\bact\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|different|new)\b'
    % (_ASK_VERB, _GAP), re.I)
# config files: only when asked for ("show the admin password from odoo.conf"), not "workers in odoo.conf?"
_RX_SECRET_FILE = re.compile(
    r'\b(?:cat|show|print|read|dump|open|display|paste|give|send|reveal|leak|bhej\w*|dikha\w*)\b[^.?!\n]{0,40}'
    r'\bodoo(?:-server)?\.conf\b|\bodoo(?:-server)?\.conf\s+(?:file\s+)?(?:dikhao|bhejo|de\s?do|do|batao|paste)\b|'
    r'(?:^|[\s/\'"`(])\.env\b|\.pem\b|\bid_rsa\b|/etc/(?:passwd|shadow)\b|\.aws/credentials', re.I)
_RX_OFF_TOPIC = re.compile(
    # "user story" / "story points" are work items, not off-topic
    r'\b(?:poems?|poetry|shayari|jokes?|lateefa|(?<!user\s)(?<!user-)(?<!user_)stor(?:y|ies)(?!\s+points?\b)|'
    r'kahani|songs?|lyrics|gaana|'
    r'recipes?\s+(?:for|of)\b(?!\s+(?:the\s+|this\s+|that\s+|a\s+)?(?:bom|product|mrp|manufactur\w*|kit)\b)|'
    r'(?:biryani|cake|curry|cooking)\s+recipes?|weather|mausam|'
    r'horoscope|zodiac|cricket|football|soccer|movies?|films?|essays?|homework|bitcoin|cryptocurrenc\w*|'
    r'capital\s+of|who\s+won|girlfriend|boyfriend|dating|breakup|love\s+(?:letter|life|advice)|'
    r'relationship\s+advice|rishta|shaadi)\b', re.I)
_RX_TERM_SPLIT = re.compile(r'[A-Za-z0-9_.]{3,}')
_RX_IDENTIFIER = re.compile(r'[._0-9]')
REFUSAL_REASONS = [('empty', 'Empty question'), ('too_long', 'Too long'), ('secret', 'Secret / prompt request'),
                   ('off_topic', 'Off-topic')]
REFUSAL_NOTICES = {
    'empty': 'Not sent: the question is empty. / Sawal khali hai.',
    'too_long': ('Not sent: the question is longer than %d characters, shorten it. / Sawal %d characters se lamba '
                 'hai, chhota karen.' % (MAX_QUESTION_CHARS, MAX_QUESTION_CHARS)),
    'secret': ('Not sent: it asks for secrets (keys, passwords, tokens, config files) or for the AI\'s own '
               'instructions / prompt. / Yeh sawal secrets (keys, passwords, tokens, config) ya AI ki apni '
               'instructions maangta hai, is liye nahi bheja gaya.'),
    'off_topic': ('Not sent: it looks unrelated to this performance issue (no earlier answer in this chat and no '
                  'term of the issue). Ask about its calls, SQL, code, fix or tests. / Yeh sawal is issue se '
                  'mutaliq nahi lagta: is issue ki calls, SQL, code, fix ya test ke baare mein poochen.'),
}


def chat_guard(question, topic_terms=(), has_history=False):
    """(True, '') when the question may go to the model, else (False, reason) with reason in
    REFUSAL_REASONS. Cheap: no model call.

    Blocks only: empty, over MAX_QUESTION_CHARS, narrow secret / prompt-extraction / injection PHRASES (never a
    single common word: "token", "shell", "ssh", "secret" inside a code question pass), and clear off-topic
    requests when the chat has no answered turn yet AND the question has no term of the issue (with history the
    model decides: short follow-ups like "aur samjhao" must pass). A COST filter, NOT a security boundary: the
    security comes from the system prompt (SECURITY_RULES), the untrusted-data framing, the read-only sandbox
    and the manager-only access."""
    text = (question or '').strip()
    if not text:
        return False, 'empty'
    if len(text) > MAX_QUESTION_CHARS:
        return False, 'too_long'
    if _RX_PROMPT_ASK.search(text) or _RX_SECRET_ASK.search(text) or _RX_SECRET_FILE.search(text):
        return False, 'secret'
    if not has_history and _RX_OFF_TOPIC.search(text) and not has_issue_term(text, topic_terms):
        return False, 'off_topic'
    return True, ''


_RX_SMALLTALK_STRIP = re.compile(r"[^a-z' ]+")
_SMALLTALK_GREETINGS = frozenset((
    'hi', 'hii', 'hiii', 'hello', 'helo', 'hey', 'heya', 'hi there', 'hello there', 'hey there', 'hi ai',
    'hello ai', 'hey ai', 'yo', 'good morning', 'good afternoon', 'good evening', 'morning',
    'salam', 'salaam', 'slam', 'aoa', 'assalamualaikum', 'assalamu alaikum', 'assalam o alaikum',
    'assalam alaikum', 'asalam o alaikum', 'asalamualaikum', 'asalam alaikum', 'as salam alaikum',
    'as salamu alaikum', 'salam alaikum', 'salamualaikum', 'walaikum salam', 'walaikum assalam',
    'wa alaikum assalam', 'adaab', 'kaise ho', 'kese ho', 'kya haal hai', 'kya hal hai', 'how are you'))
_SMALLTALK_THANKS = frozenset((
    'thanks', 'thank you', 'thanks a lot', 'thank you so much', 'thanks so much', 'many thanks', 'thx', 'ty',
    'tnx', 'shukriya', 'shukria', 'shukrya', 'bohat shukriya', 'bahut shukriya', 'bohot shukriya', 'jazakallah',
    'jazak allah', 'ok', 'okay', 'okk', 'ok thanks', 'okay thanks', 'ok thank you', 'ok shukriya', 'got it',
    'great', 'nice', 'cool', 'good', 'perfect', 'great thanks', 'theek hai', 'thik hai', 'theek', 'acha',
    'achha', 'accha', 'acha shukriya', 'done', 'noted'))
SMALLTALK_REPLIES = {
    'greeting': 'Hi! Ask me about this issue: its calls, SQL, code, the fix or how to verify it. / '
                'Is issue ke baare mein poochen.',
    'thanks': 'You are welcome! Ask me anything else about this issue. / Koi aur sawal ho to poochen.',
}


def chat_smalltalk(question):
    """Canned reply (str) when ``question`` is ONLY a greeting / thanks / "ok" (English or Roman Urdu), else None.
    Exact match of the normalized text (lower case, punctuation / emoji removed, spaces collapsed), never a
    substring match: "hi, why is write slow" is a real question and goes the normal route."""
    text = _RX_SMALLTALK_STRIP.sub(' ', (question or '').lower().replace("\u2019", "'"))
    text = ' '.join(text.replace("'", '').split())
    if not text or len(text) > 40:
        return None
    if text in _SMALLTALK_GREETINGS:
        return SMALLTALK_REPLIES['greeting']
    if text in _SMALLTALK_THANKS:
        return SMALLTALK_REPLIES['thanks']
    return None


def has_issue_term(text, topic_terms):
    """True when ``text`` names an identifier of the issue (model, table, method with '_', call site file):
    only identifier-like terms count (with '.', '_' or a digit), not plain words such as a method 'write'."""
    lower = (text or '').lower()
    return any(t.lower().strip('.') in lower for t in topic_terms
               if t and len(t.strip('.')) >= 3 and _RX_IDENTIFIER.search(t.strip('.')))


def topic_terms_of(values):
    """Identifiers (>= 3 characters) of the issue values (model, method, route, call site, tables ...)."""
    terms = set()
    for value in values:
        terms.update(_RX_TERM_SPLIT.findall(value or ''))
    return terms


# ---------------------------------------------------------------------------- call site
# frames that are never the interesting caller: wags_logger patches the cursor (every query passes through
# wags_logger/models/wags_logger.py _patched_cursor_execute), the SQL / ORM layers, the monitors themselves
SKIPPED_FRAME_PARTS = ('/wags_logger/', '/perf_monitor/collector/', '/perf_monitor_agent/', '/odoo/sql_db.py',
                       '/odoo/models.py')
_RX_FRAME = re.compile(r'([\w./-]+\.py):(\d+)(?:\s*\([^)]*\))?')


def is_skipped_frame(path):
    path = '/' + (path or '').replace('\\', '/').lstrip('/')
    return any(part in path for part in SKIPPED_FRAME_PARTS)


def real_callsite(callsite):
    """First application frame of a call site text ('path.py:line (func)', or several frames separated by
    anything): wags_logger / SQL layer / monitor frames are skipped. '' when every frame is skipped; the text
    unchanged when it has no recognisable frame."""
    text = (callsite or '').strip()
    frames = list(_RX_FRAME.finditer(text))
    if not frames:
        return text
    for match in frames:
        if not is_skipped_frame(match.group(1)):
            return match.group(0).strip()
    return ''


def best_callsite(candidates):
    """First real call site among ``candidates`` (ordered: slowest call first), else ''."""
    for candidate in candidates:
        real = real_callsite(candidate)
        if real:
            return real
    return ''


# ---------------------------------------------------------------------------- tokens
_ENCODER = []   # [encoder or None]: tiktoken is tried once per process, only when asked for


def _encoder(load=False):
    """The tiktoken encoder when already loaded in this process; ``load``: try to load it once (import +
    get_encoding, which downloads the encoding file when it is not cached: never on a form open)."""
    if not _ENCODER and load:
        encoder = None
        try:
            import tiktoken  # pylint: disable=import-outside-toplevel
            encoder = tiktoken.get_encoding('cl100k_base')
        except Exception:  # pylint: disable=broad-except  # not installed, or no cached encoding (offline)
            encoder = None
        _ENCODER.append(encoder)
    return _ENCODER[0] if _ENCODER else None


def estimate_text_tokens(text, use_tiktoken=False):
    """(tokens, method): characters / 4, or tiktoken cl100k_base (an approximation of Claude's tokenizer) when
    ``use_tiktoken`` (Settings parameter perf_monitor.ai_tiktoken) and importable. Display only: the Bedrock
    usage (inputTokens / outputTokens) stays authoritative."""
    text = text or ''
    encoder = _encoder(load=True) if use_tiktoken else None
    if encoder is not None:
        try:
            return len(encoder.encode(text, disallowed_special=())), 'tiktoken'
        except Exception:  # pylint: disable=broad-except
            pass
    return (len(text) + 3) // 4, 'chars/4'


def size_label(text):
    """'12,345 characters, ~3,086 tokens (estimate, chars/4)': forms only, never loads tiktoken."""
    tokens, method = estimate_text_tokens(text)
    return '{:,} characters, ~{:,} tokens (estimate, {})'.format(len(text or ''), tokens, method)


def clip_item(text, size=ROUND_ITEM_CHARS):
    text = text or ''
    return text if len(text) <= size else text[:size] + '\n[... %d more characters not stored]' % (len(text) - size)


def rounds_log(messages):
    """Readable log of the tool rounds of a run: the assistant turns (text, tool calls) and the tool results
    sent back, every item clipped to ROUND_ITEM_CHARS. ``messages``: the Converse messages AFTER the first
    prompt (exactly what was sent / received)."""
    out, number = [], 0
    for message in messages:
        role = message.get('role')
        if role == 'assistant':
            number += 1
            out.append('=== round %d: model answer' % number)
        else:
            out.append('--- sent back to the model')
        for block in message.get('content') or []:
            if not isinstance(block, dict):
                continue
            if block.get('text'):
                out.append(clip_item(block['text']))
            elif 'toolUse' in block:
                use = block['toolUse']
                out.append(clip_item('tool call %s %s' % (use.get('name'), json.dumps(
                    use.get('input') or {}, ensure_ascii=False, default=str))))
            elif 'toolResult' in block:
                result = block['toolResult']
                body = '\n'.join(c.get('text', '') for c in result.get('content') or [] if isinstance(c, dict))
                out.append(clip_item('tool result (%s):\n%s' % (result.get('status'), body)))
    return '\n'.join(out)


def messages_text(messages):
    """The user / assistant turns of a Converse request as text (chat history + question)."""
    out = []
    for message in messages:
        texts = [b.get('text', '') for b in message.get('content') or [] if isinstance(b, dict) and b.get('text')]
        out.append('[%s]\n%s' % (message.get('role'), '\n'.join(texts)))
    return '\n\n'.join(out)


# ---------------------------------------------------------------------------- HTML
_PRE_STYLE = ('white-space: pre-wrap; word-break: break-word; max-height: 70vh; overflow: auto; '
              'font-size: 12px; background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 4px; padding: 8px;')
_COPY_JS = ("var p=this.parentNode.querySelector('pre');if(p&&navigator.clipboard){"
            "navigator.clipboard.writeText(p.innerText);this.innerText='Copied';}")


def pre_block(title, text, copy=False):
    """A scrollable, monospace block of escaped text (never truncated), with a title line giving its size
    (characters, estimated tokens) when ``title`` is set."""
    text = text or ''
    button = ('<button type="button" class="btn btn-sm btn-secondary float-end" onclick="%s">Copy</button>'
              % html.escape(_COPY_JS)) if copy and text else ''
    header = ('<div class="fw-bold">%s <span class="text-muted small fw-normal">(%s)</span></div>' % (
        html.escape(title), html.escape(size_label(text)))) if title else ''
    return '<div class="mb-3">%s%s<pre class="font-monospace" style="%s">%s</pre></div>' % (
        button, header, _PRE_STYLE, html.escape(text) or '<span class="text-muted">(empty)</span>')


_RX_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_RX_BOLD = re.compile(r'\*\*([^*\n]+)\*\*')
_RX_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_RX_BULLET = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+(.*)$')


def _inline(text):
    text = html.escape(text)
    text = _RX_INLINE_CODE.sub(lambda m: '<code>%s</code>' % m.group(1), text)
    return _RX_BOLD.sub(lambda m: '<strong>%s</strong>' % m.group(1), text)


def _code_html(lang, lines):
    diff = lang in ('diff', 'patch') or any(l.startswith(('+++', '---', '@@')) for l in lines)
    rendered = []
    for line in lines:
        escaped = html.escape(line)
        if diff and line.startswith('+') and not line.startswith('+++'):
            escaped = '<span style="background:#e6ffec;display:block;">%s</span>' % escaped
        elif diff and line.startswith('-') and not line.startswith('---'):
            escaped = '<span style="background:#ffebe9;display:block;">%s</span>' % escaped
        elif diff and line.startswith('@@'):
            escaped = '<span style="color:#6f42c1;display:block;">%s</span>' % escaped
        else:
            escaped += '\n'
        rendered.append(escaped)
    return ('<div class="o_perf_code position-relative my-2"><button type="button" class="o_perf_code_copy btn '
            'btn-sm btn-light border position-absolute end-0 top-0 m-1" onclick="%s">Copy</button><pre '
            'class="font-monospace mb-0" style="%s">%s</pre></div>' % (html.escape(_COPY_JS), _PRE_STYLE,
                                                                      ''.join(rendered)))


def render_markdown(text):
    """Markdown-ish HTML of an AI answer: ``` code blocks (diff lines coloured, Copy button), headings, bullets,
    **bold**, `code`. Every piece of text is HTML-escaped."""
    out, para, items = [], [], []
    lines = (text or '').splitlines()

    def flush():
        if para:
            out.append('<p class="mb-2">%s</p>' % '<br/>'.join(_inline(l) for l in para))
            del para[:]
        if items:
            out.append('<ul class="mb-2">%s</ul>' % ''.join('<li>%s</li>' % _inline(i) for i in items))
            del items[:]
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = line.strip()
        if fence.startswith('```'):
            flush()
            lang = fence[3:].strip().lower()
            code, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code.append(lines[i])
                i += 1
            out.append(_code_html(lang, code))
            i += 1
            continue
        heading, bullet = _RX_HEADING.match(line), _RX_BULLET.match(line)
        if heading:
            flush()
            out.append('<div class="fw-bold mt-2">%s</div>' % _inline(heading.group(2)))
        elif bullet:
            if para:
                flush()
            items.append(bullet.group(1))
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line)
        i += 1
    flush()
    return ''.join(out)


CHAT_EXAMPLES = ('Who calls this method?', 'What is the smallest safe fix?', 'How do I verify the fix?')
_NOTICE_CSS = {'refused': 'o_perf_chat_notice_warning', 'limit': 'o_perf_chat_notice_warning',
               'error': 'o_perf_chat_notice_danger'}


def _chat_empty_html():
    chips = ''.join('<button type="button" class="o_perf_chat_chip" data-question="%s">%s</button>'
                    % (html.escape(q), html.escape(q)) for q in CHAT_EXAMPLES)
    return ('<div class="o_perf_chat_thread o_perf_chat_thread_empty"><div class="o_perf_chat_empty">'
            '<div class="o_perf_chat_empty_icon"><i class="fa fa-magic" aria-hidden="true"></i></div>'
            '<div class="o_perf_chat_empty_title">Ask anything about this issue</div>'
            '<div class="o_perf_chat_empty_sub">Its calls, SQL, code, the fix or how to verify it. / '
            'Is issue ke baare mein kuch bhi poochen.</div>'
            '<div class="o_perf_chat_chips">%s</div></div></div>' % chips)


def render_chat_html(messages):
    """Chat thread (classes o_perf_chat_*, static/src/scss/perf_chat.scss): ``messages`` = list of dicts (role
    'user' | 'assistant' | 'notice', content, state, author, date, model, footnote). Questions = right bubbles,
    AI answers = left with avatar and model label (markdown-ish), notices = centred pills, no message = the
    greeting with example questions. Every piece of message text is HTML-escaped (untrusted)."""
    if not messages:
        return _chat_empty_html()
    out = ['<div class="o_perf_chat_thread">']
    for msg in messages:
        role, state = msg.get('role'), msg.get('state') or 'ok'
        content = msg.get('content') or ''
        if role == 'user':
            meta = ' · '.join(p for p in (msg.get('author'), msg.get('date')) if p)
            out.append(
                '<div class="o_perf_chat_row o_perf_chat_row_user"><div class="o_perf_chat_bubble">%s</div>'
                '<div class="o_perf_chat_meta">%s%s</div></div>' % (
                    html.escape(content), html.escape(meta),
                    ' · <span class="o_perf_chat_refused">refused</span>' if state == 'refused' else ''))
        elif role == 'assistant':
            badge = '' if state == 'ok' else '<span class="o_perf_chat_badge">%s</span>' % html.escape(state)
            foot = ' · '.join(p for p in (msg.get('footnote'), msg.get('date')) if p)
            out.append(
                '<div class="o_perf_chat_row o_perf_chat_row_ai"><div class="o_perf_chat_avatar">'
                '<i class="fa fa-magic" aria-hidden="true"></i></div><div class="o_perf_chat_ai">'
                '<div class="o_perf_chat_ai_name">%s%s</div><div class="o_perf_chat_answer">%s</div>'
                '<div class="o_perf_chat_meta">%s</div></div></div>' % (
                    html.escape(msg.get('model') or 'Assistant'), badge, render_markdown(content),
                    html.escape(foot)))
        else:
            out.append('<div class="o_perf_chat_row o_perf_chat_row_notice"><div class="o_perf_chat_notice %s" '
                       'role="status">%s<span class="o_perf_chat_notice_date">%s</span></div></div>' % (
                           _NOTICE_CSS.get(state, ''), html.escape(content), html.escape(msg.get('date') or '')))
    out.append('</div>')
    return ''.join(out)
