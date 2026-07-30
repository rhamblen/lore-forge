"""Getting JSON out of a local model without losing your mind.

A 12B model asked for JSON returns JSON *most* of the time. The rest of the time it
returns JSON wrapped in a ```json fence, or preceded by "Sure! Here's the extraction:",
or with a trailing comma, or with smart quotes because the source text had them, or
truncated halfway because it hit the token limit. `json.loads` rejects all of it.

Retrying the model is the expensive fix and it is not even reliable — the same prompt
tends to produce the same malformation. Repairing the text is cheap, deterministic, and
testable without a GPU, which is why this module exists separately from `extract.py` and
carries the heaviest test coverage in the project.

The one rule: **repair only what is unambiguous.** Stripping a fence or a trailing comma
cannot change meaning. Guessing at a truncated object can, so truncation is reported as a
failure rather than patched — a silently invented rule is far worse than a missing one.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ```json ... ``` or ``` ... ```
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
# Typographic quotes, which arrive when the model echoes text from the book
_SMART = {
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‘": "'", "’": "'", "‚": "'",
}


class JSONRepairError(ValueError):
    """Raised when the text cannot be turned into JSON without guessing."""


def _strip_fence(text: str) -> str:
    m = _FENCE.search(text)
    return m.group(1) if m else text


def _balanced_slice(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the first balanced `open_ch...close_ch` region, ignoring brackets that
    appear inside strings. A naive `text[text.find('{'):text.rfind('}')+1]` breaks the
    moment a quoted value contains a brace — which prose routinely does."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # never closed — truncated output


def coerce_json(text: str) -> Any:
    """Parse model output into a Python object, repairing only the unambiguous.

    Raises JSONRepairError if the text is truncated or simply isn't JSON.
    """
    if not text or not text.strip():
        raise JSONRepairError("model returned nothing")

    candidate = _strip_fence(text).strip()

    # Fast path: it's already valid.
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Isolate the JSON value from any surrounding chatter. Try object first, then array,
    # preferring whichever starts earlier so a list-of-objects isn't mistaken for the
    # inside of an object.
    obj = _balanced_slice(candidate, "{", "}")
    arr = _balanced_slice(candidate, "[", "]")
    if obj and arr:
        body = obj if candidate.index(obj) <= candidate.index(arr) else arr
    else:
        body = obj or arr

    if body is None:
        # Distinguish "truncated" from "not JSON at all" — the caller may want to retry
        # with a smaller input in the first case and give up in the second.
        if "{" in candidate or "[" in candidate:
            raise JSONRepairError(
                "output is truncated — an opening brace was never closed. Reduce the "
                "input size or raise the model's output limit; do not guess the rest.")
        raise JSONRepairError("no JSON value found in the model output")

    for repair in (lambda s: s,
                   _strip_trailing_commas,
                   lambda s: _strip_trailing_commas(_desmarten(s))):
        try:
            return json.loads(repair(body))
        except json.JSONDecodeError:
            continue

    raise JSONRepairError("output could not be parsed as JSON even after repair")


def _scan_strings(s: str):
    """Yield `(index, char, in_string)` for every character.

    Shared by both repairs, because both must leave string *contents* alone. A regex
    cannot do this: `,(\\s*[}\\]])` happily eats the comma inside the value
    `"a ,} b"` and silently changes the extracted text.
    """
    in_string = False
    escaped = False
    for i, ch in enumerate(s):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            yield i, ch, True
            continue
        if ch == '"':
            in_string = True
            yield i, ch, False   # the opening quote itself is structural
            continue
        yield i, ch, False


def _strip_trailing_commas(s: str) -> str:
    """Remove commas that sit immediately before a closing brace or bracket, ignoring
    any comma inside a string."""
    drop: set[int] = set()
    pending: int | None = None          # index of a comma awaiting its next real char
    for i, ch, in_str in _scan_strings(s):
        if in_str:
            pending = None
            continue
        if ch == ",":
            pending = i
        elif ch.isspace():
            continue                     # whitespace may sit between , and }
        else:
            if pending is not None and ch in "}]":
                drop.add(pending)
            pending = None
    if not drop:
        return s
    return "".join(c for i, c in enumerate(s) if i not in drop)


def _desmarten(s: str) -> str:
    """Replace typographic quotes with ASCII — but only OUTSIDE strings, so a quote that
    is legitimately part of a value survives. Getting this backwards would corrupt the
    book's own punctuation in extracted text."""
    out = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        else:
            out.append(_SMART.get(ch, ch))
    return "".join(out)


def coerce_object(text: str) -> dict[str, Any]:
    """coerce_json, insisting on an object."""
    value = coerce_json(text)
    if isinstance(value, list):
        # A model told "return an object with a `rules` key" quite often returns the bare
        # list instead. That is unambiguous enough to accept.
        return {"_list": value}
    if not isinstance(value, dict):
        raise JSONRepairError(f"expected a JSON object, got {type(value).__name__}")
    return value


def coerce_list(text: str, key: str) -> list[Any]:
    """Get a list out of the model, whether it wrapped it in `key` or not.

    Small models flip between `{"rules": [...]}` and a bare `[...]` for the same prompt,
    so accepting both removes an entire class of retry.
    """
    value = coerce_json(text)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if isinstance(value.get(key), list):
            return value[key]
        # Single object where a list was asked for — also common, also unambiguous.
        if key not in value and value:
            return [value]
        return []
    raise JSONRepairError(f"expected a list, got {type(value).__name__}")
