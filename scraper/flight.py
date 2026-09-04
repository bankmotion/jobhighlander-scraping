"""Reading Next.js App Router "RSC flight" payloads.

Sites built on the Next.js App Router ship every page twice: once as rendered
HTML, and once as the serialised React payload the server used to produce it.
That payload arrives as a run of script tags:

    <script>self.__next_f.push([1,"<chunk>"])</script>

Concatenating the chunks reconstructs a stream of newline-separated records:

    <id>:<json>            an ordinary value
    <id>:T<hexlen>,<text>  a raw text blob (job descriptions arrive this way)

and values inside the JSON may REFERENCE another record by id — `"$27"` means
"see record 27". So a field can hold the string "$27" rather than the 4KB of
HTML you actually want; `text_chunk` resolves those.

Reading the payload beats scraping the DOM: it is the site's own structured data,
with exact timestamps and ids the rendered page only ever shows approximations
of. Shared by scraper/dice.py and scraper/ziprecruiter.py.
"""
from __future__ import annotations

import json
import re
from typing import Optional

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1\s*,\s*("(?:[^"\\]|\\.)*")')


def flight_text(html: str) -> str:
    """Reassemble the flight stream from a page's script chunks.

    Each chunk is a JSON string literal, so `json.loads` handles the escaping.
    A chunk that fails to parse is skipped rather than poisoning the rest.

    Already-decoded input (an RSC endpoint returns the stream directly, with no
    script tags) is passed through unchanged, so callers can hand us either.
    """
    if "self.__next_f" not in (html or ""):
        return html or ""
    out = []
    for m in _PUSH_RE.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:
            continue
    return "".join(out)


def flight_value(text: str, key: str, start: int = 0):
    """The JSON object/array that follows `"<key>":`, or None.

    Bracket-matched rather than regex-captured: these values nest arbitrarily
    and a regex cannot find their real end. String contents are skipped so a
    brace inside a job description can't close the object early.
    """
    i = text.find('"' + key + '":', start)
    if i < 0:
        return None
    j = i + len(key) + 3
    while j < len(text) and text[j] in " \t":
        j += 1
    if j >= len(text) or text[j] not in "[{":
        return None
    open_c = text[j]
    close_c = "]" if open_c == "[" else "}"
    depth, k, in_str, esc = 0, j, False, False
    while k < len(text):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except Exception:
                    return None
        k += 1
    return None


def text_chunk(text: str, ref: str) -> Optional[str]:
    """Resolve a text reference like "$27" to the blob in record `27`.

    The record is `27:T<hexlen>,<content>` and the length prefix is what makes
    this exact: the content is arbitrary HTML that may contain newlines, so
    reading to the next line break would truncate it. Returns None if `ref`
    isn't a reference (the caller then already has its value).
    """
    if not isinstance(ref, str) or not ref.startswith("$") or len(ref) < 2:
        return None
    cid = re.escape(ref[1:])
    m = re.search(r"(?:^|\n)" + cid + r":T([0-9a-fA-F]+),", text)
    if not m:
        return None
    try:
        n = int(m.group(1), 16)
    except ValueError:
        return None
    return text[m.end():m.end() + n]
