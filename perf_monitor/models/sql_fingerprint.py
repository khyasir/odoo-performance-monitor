# -*- coding: utf-8 -*-
"""SQL normalizer / fingerprint shared by perf_monitor and perf_monitor_agent.

Pure stdlib, no Odoo import. This file is duplicated VERBATIM as
``perf_monitor_agent/tools/sql_fingerprint.py`` (the agent must stay installable without
perf_monitor); ``collector_tests`` asserts both copies are identical and both modules test the same
samples against the same expected fingerprints.

The same statement must give the same fingerprint whether it comes from
* the Odoo query template seen by ``Cursor.execute`` (``%s`` / ``%(name)s`` placeholders, the
  agent), or
* pg_stat_statements / pg_stat_activity (``$1`` placeholders, psycopg2 inlines tuples as
  ``IN ($1, $2, $3)``), possibly already redacted by ``redact_sql`` (``?``, ``IN (?)``).
"""
import hashlib
import re

# --- BEGIN shared: sql_fingerprint (keep identical in perf_monitor_agent/tools/sql_fingerprint.py) ---
_RX_TOKEN = re.compile(r"""
      (?P<lcomment>--[^\n]*)
    | (?P<bcomment>/\*.*?(?:\*/|\Z))
    | (?P<estring>[eE]'(?:[^'\\]|\\.|'')*(?:'|\Z))
    | (?P<string>(?:[bBxXnN]|[uU]&)?'(?:[^']|'')*(?:'|\Z))
    | (?P<dollar>\$(?P<tag>[A-Za-z_]\w*)?\$.*?(?:\$(?P=tag)?\$|\Z))
    | (?P<ident>"(?:[^"]|"")*"?)
    | (?P<param>\$[0-9]+|%\([^)]*\)s|%s|\?)
    | (?P<word>[^\W\d]\w*)
    | (?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)
""", re.S | re.X)
_RX_WS = re.compile(r"\s+")
_RX_PUNCT_SPACE = re.compile(r"\s*([(),])\s*")
_RX_Q_LIST = r"(?:\?|\((?:\?,)*\?\)|array\[(?:\?,)*\?\])"
_RX_IN_LIST = re.compile(r"\bin ?" + _RX_Q_LIST)
_RX_ANY_LIST = re.compile(r"\bany\(" + _RX_Q_LIST + r"\)")
_RX_VALUES = re.compile(r"\bvalues ?" + _RX_Q_LIST + r"(?:," + _RX_Q_LIST + r")*")
MAX_FINGERPRINT_INPUT = 20000


def _replace_token(match):
    kind = match.lastgroup
    if kind in ("lcomment", "bcomment"):
        return " "
    if kind in ("estring", "string", "dollar", "param", "number"):
        return "?"
    return match.group()


def normalize_sql(text):
    """Canonical form used for fingerprints: comments dropped, string / number literals and $n,
    %s, %(name)s, ? placeholders -> ``?``, lower case, whitespace collapsed (none around ``( ) ,``),
    ``IN (?, ?, ...)`` / ``IN %s`` / ``= ANY(ARRAY[...])`` -> ``in (...)`` / ``any((...))`` and
    ``VALUES (...), (...)`` -> ``values (...)``, no trailing ``;``. Identifiers are kept."""
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    elif not isinstance(text, str):
        text = str(text)
    text = _RX_TOKEN.sub(_replace_token, text[:MAX_FINGERPRINT_INPUT])
    text = _RX_WS.sub(" ", text.lower()).strip()
    text = _RX_PUNCT_SPACE.sub(r"\1", text)
    text = _RX_IN_LIST.sub("in (...)", text)
    text = _RX_ANY_LIST.sub("any((...))", text)
    text = _RX_VALUES.sub("values (...)", text)
    # again: "IN %s" -> "in (...)" must give the same spacing as "IN ($1, $2)" -> "in (...)"
    text = _RX_PUNCT_SPACE.sub(r"\1", text)
    return text.rstrip("; ")


def sql_fingerprint(text):
    """sha1 hex of normalize_sql(text); '' for an empty statement."""
    norm = normalize_sql(text)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest() if norm else ""


def redact_literals(text):
    """Literal values -> ``?`` (comments dropped), keeping case and layout: safe to store/show."""
    if not text:
        return text or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    elif not isinstance(text, str):
        text = str(text)
    return _RX_TOKEN.sub(lambda m: "/*?*/" if m.lastgroup in ("lcomment", "bcomment")
                         else "?" if m.lastgroup in ("estring", "string", "dollar", "number")
                         else m.group(), text)
# --- END shared ---
