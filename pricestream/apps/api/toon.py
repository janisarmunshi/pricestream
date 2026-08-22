"""
Minimal TOON (Token-Oriented Object Notation) encoder, scoped to exactly the shape
the tick export needs: a single top-level key holding a uniform array of flat
objects (the "tabular form", spec §9.3) — e.g.

    ticks[2]{time,token,ltp}:
      2026-08-22T10:15:00Z,22,1234.5
      2026-08-22T10:15:01Z,22,1234.6

Implements the encoding rules from the TOON spec (github.com/toon-format/spec,
v4.1): §7.2 quoting conditions, §7.1 escape table, §2 number/null/boolean
canonical form, §12 indentation (2 spaces, no tabs). Does not implement the full
spec (nested objects, non-uniform arrays, list form) — not needed for tick rows,
which are always flat and uniform.
"""
import math
import re

_NUMERIC_RE = re.compile(r'^[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$')
_CONTROL_CHARS = {chr(c) for c in range(0x20)}
_ESCAPES = {
    '\\': '\\\\',
    '"': '\\"',
    '\n': '\\n',
    '\r': '\\r',
    '\t': '\\t',
}


def _needs_quoting(s: str) -> bool:
    if s == '':
        return True
    if s != s.strip():
        return True
    if s in ('true', 'false', 'null'):
        return True
    if _NUMERIC_RE.match(s):
        return True
    if any(ch in s for ch in (':', '"', '\\', '[', ']', '{', '}', ',')):
        return True
    if any(ch in _CONTROL_CHARS for ch in s):
        return True
    if s == '-' or s.startswith('-'):
        return True
    if s == '#' or s.startswith('#'):
        return True
    return False


def _escape_string(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch in _CONTROL_CHARS:
            out.append(f'\\u{ord(ch):04x}')
        else:
            out.append(ch)
    return ''.join(out)


def _encode_number(n) -> str:
    if isinstance(n, bool):  # bool is a subclass of int — never reach the number path
        return 'true' if n else 'false'
    if isinstance(n, float) and (math.isnan(n) or math.isinf(n)):
        return 'null'
    if n == 0:
        n = 0  # normalize -0 -> 0
    if isinstance(n, int):
        return str(n)
    # float: canonical form — integer form if fractional part is zero, no trailing
    # zeros, no exponent within 1e-6 <= |n| < 1e21 per spec §2.
    if n == int(n) and abs(n) < 1e21:
        return str(int(n))
    text = repr(float(n))
    if 'e' not in text and 'E' not in text:
        return text.rstrip('0').rstrip('.') if '.' in text else text
    return text


def _encode_scalar(value) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return _encode_number(value)
    s = str(value)
    if _needs_quoting(s):
        return f'"{_escape_string(s)}"'
    return s


def encode_tabular(key: str, rows: list, fields: list) -> str:
    """Encode `rows` (a list of dicts, all sharing `fields`) as one TOON tabular
    array under `key`. Every row is expected to provide every field — pass None
    for a genuinely absent value rather than omitting the key.
    """
    header = f'{key}[{len(rows)}]{{{",".join(fields)}}}:'
    lines = [header]
    for row in rows:
        values = [_encode_scalar(row.get(f)) for f in fields]
        lines.append('  ' + ','.join(values))
    return '\n'.join(lines) + '\n'
