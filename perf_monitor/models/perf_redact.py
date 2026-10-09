# -*- coding: utf-8 -*-
"""Pure-stdlib helpers shared with the collector agent.

The collector must stay a single standalone file, so the block between the BEGIN/END markers is
duplicated verbatim in ``collector/perf_collector.py``; ``collector_tests`` asserts that both copies
are identical and behave the same. Do not import Odoo here.
"""
import re
import urllib.parse

# --- BEGIN shared: redact_sql / is_allowed_central_url (keep identical in perf_collector.py) ---
_RX_WS = re.compile(r"\s+")
_RX_IDENT = re.compile(r"[^\W\d][\w$]*")
_RX_NUMBER = re.compile(
    r"(?:0[xX][0-9A-Fa-f_]+|0[oO][0-7_]+|0[bB][01_]+"
    r"|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9]+)?)\w*")
_RX_PARAM = re.compile(r"\$[0-9]+")
_RX_PYPARAM = re.compile(r"%(?:\([^)]*\))?s")
_RX_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")
_RX_COMMENT_MARK = re.compile(r"/\*|\*/")
_STRING_PREFIXES = ("E", "B", "X", "N")
_LITERAL_WORDS = ("NULL", "TRUE", "FALSE", "DEFAULT")
_SAFE_CENTRAL_URL = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+(/[A-Za-z0-9._\-/]*)?$")
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _skip_string(text, i, backslash):
    """Index just after the single-quoted string opening at text[i] (len(text) if unterminated)."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def redact_sql(text):
    """Replace literal values in SQL text by ``?``; keep identifiers, keywords and placeholders.

    Single pass tokenizer. Redacted: '...' strings ('' escapes), E'...' (backslash escapes), U&'...',
    B'...', X'...', N'...', $$...$$ / $tag$...$tag$ bodies, numeric literals (not digits inside
    identifiers such as foo1 or t2.col; $1 / %s / %(name)s placeholders are kept), IN (<literals or
    placeholders>) -> IN (?) (IN (SELECT ...) is kept), and everything after the keyword PASSWORD
    -> ``PASSWORD ?``. Double-quoted identifiers are kept; comment bodies are dropped (``--?``,
    ``/*?*/``; nested block comments are followed to the matching ``*/``). Truncated input is
    handled: an unterminated string / dollar quote / comment is redacted up to the end.
    """
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    out = []
    # one frame per open parenthesis: [is_in_list, out index of "(", only_literals, has_literal]
    stack = []
    prev_word = None  # last significant token when it is a word (upper case), for "IN ("
    cast = False  # inside "::type name" (type words count as literals within IN lists)
    n = len(text)
    i = 0

    def mark(kind):
        if stack and stack[-1][0]:
            if kind == "lit":
                stack[-1][3] = True
            elif kind == "other":
                stack[-1][2] = False

    while i < n:
        c = text[i]
        if c.isspace():
            m = _RX_WS.match(text, i)
            out.append(m.group())
            i = m.end()
            continue
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            out.append("--?")
            continue
        if c == "/" and text.startswith("/*", i):
            # PostgreSQL block comments nest: skip until the depth is back to 0 (or the end)
            depth, i = 1, i + 2
            while depth:
                m = _RX_COMMENT_MARK.search(text, i)
                if not m:
                    i = n
                    break
                depth += 1 if m.group() == "/*" else -1
                i = m.end()
            out.append("/*?*/")
            continue
        if c == "'":
            i = _skip_string(text, i, False)
            out.append("?")
            mark("lit")
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            mark("lit" if cast else "other")
        elif c == "$":
            m = _RX_PARAM.match(text, i) or _RX_DOLLAR_TAG.match(text, i)
            if m and "0" <= m.group()[1:2] <= "9":
                out.append(m.group())
                i = m.end()
                mark("lit")
            elif m:
                j = text.find(m.group(), m.end())
                i = n if j < 0 else j + len(m.group())
                out.append("?")
                mark("lit")
            else:
                out.append(c)
                i += 1
                mark("other")
        elif "0" <= c <= "9" or (c == "." and i + 1 < n and "0" <= text[i + 1] <= "9"):
            m = _RX_NUMBER.match(text, i)
            out.append("?")
            i = m.end()
            mark("lit")
        elif c == "%" and _RX_PYPARAM.match(text, i):
            m = _RX_PYPARAM.match(text, i)
            out.append(m.group())
            i = m.end()
            mark("lit")
        elif c == "?":
            out.append(c)
            i += 1
            mark("lit")
        elif _RX_IDENT.match(text, i):
            m = _RX_IDENT.match(text, i)
            j = m.end()
            upper = m.group().upper()
            if j < n and text[j] == "'" and upper in _STRING_PREFIXES:
                i = _skip_string(text, j, upper == "E")
                out.append("?")
                mark("lit")
            elif upper == "U" and text.startswith("&'", j):
                i = _skip_string(text, j + 1, False)
                out.append("?")
                mark("lit")
            elif upper == "PASSWORD":
                out.append("PASSWORD ?")
                return "".join(out)
            else:
                out.append(m.group())
                i = j
                mark("lit" if cast or upper in _LITERAL_WORDS else "other")
                prev_word = upper
                continue  # a word keeps a pending cast active ("::character varying")
        elif c == "(":
            mark("other")
            stack.append([prev_word == "IN", len(out), True, False])
            out.append(c)
            i += 1
        elif c == ")":
            frame = stack.pop() if stack else None
            if frame and frame[0] and frame[2] and frame[3]:
                del out[frame[1]:]
                out.append("(?)")
            else:
                out.append(c)
            i += 1
        elif c == ":" and text.startswith("::", i):
            out.append("::")
            i += 2
            prev_word = None
            cast = True
            continue
        else:
            out.append(c)
            i += 1
            if c not in ",+-":
                mark("other")
        prev_word = None
        cast = False
    return "".join(out)


def is_allowed_central_url(url):
    """https:// anywhere; plain http:// only for the loopback hosts 127.0.0.1, [::1] and localhost."""
    if not isinstance(url, str) or not _SAFE_CENTRAL_URL.match(url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    if not host:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in _LOOPBACK_HOSTS
# --- END shared ---
