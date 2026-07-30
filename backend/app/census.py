"""Character census — pass 1 of two, and almost entirely model-free.

You cannot write a character sheet chunk by chunk. A rule is stated in one place; a
character is distributed across forty chapters. So characters get two passes:

    pass 1 (here)   who exists, what they are called, how much they matter
    pass 2 (later)  per character, retrieve their passages and write ONE coherent sheet

This module is pass 1, and it is deliberately lexical. Asking a 12B model "who are the
characters?" 231 times is slow, expensive, and worse at counting than a regex. Names are
*surface features* — capitalisation and dialogue attribution find them reliably — so the
engine harvests and counts, and the model is spent only on the genuinely hard part
(deciding which surface forms are the same person, and which are not people at all).

**The tier is computed, never asked for.** Database = truth: mention counts, chapter
spread and dialogue attribution are evidence. "Is this character important?" is a
question a model will answer confidently and inconsistently, so it is not asked.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# Verbs that mark attributed speech. The single best signal that a name belongs to a
# person who *talks* — which is most of the difference between a character who needs a
# card and one who needs a lorebook line.
_SPEECH_VERBS = (
    "said|asked|replied|answered|shouted|whispered|muttered|murmured|growled|snapped|"
    "called|added|continued|explained|admitted|offered|insisted|repeated|sighed|laughed|"
    "yelled|hissed|breathed|declared|demanded|observed|remarked|responded|retorted"
)

# "<Name> said" and "said <Name>" — both orders, one to three capitalised words.
_NAME = r"[A-Z][a-z’'\-]+(?:\s+[A-Z][a-z’'\-]+){0,2}"
_SPEAKER_BEFORE = re.compile(rf"\b({_NAME})\s+(?:{_SPEECH_VERBS})\b")
_SPEAKER_AFTER = re.compile(rf"\b(?:{_SPEECH_VERBS})\s+({_NAME})\b")

# Any capitalised run — the raw candidate pool. Recall-biased on purpose: a name missed
# here can never be recovered downstream, while a false positive is dropped by the
# frequency floor or by the model.
_CAPITALISED = re.compile(rf"\b({_NAME})\b")

# Words that start sentences or head titles and would otherwise flood the pool. Not a
# name blacklist — a real character called "Will" survives on frequency and dialogue.
_STOPWORDS = {
    "the", "a", "an", "and", "but", "or", "if", "so", "then", "when", "while", "after",
    "before", "because", "though", "although", "since", "until", "unless", "as", "at",
    "by", "for", "from", "in", "into", "of", "on", "onto", "out", "over", "to", "up",
    "with", "without", "he", "she", "it", "they", "we", "you", "i", "his", "her", "its",
    "their", "our", "your", "my", "this", "that", "these", "those", "there", "here",
    "what", "which", "who", "whom", "whose", "why", "how", "not", "no", "yes", "all",
    "any", "both", "each", "few", "more", "most", "other", "some", "such", "only",
    "own", "same", "than", "too", "very", "can", "will", "just", "now", "well", "even",
    "still", "back", "down", "off", "again", "once", "one", "two", "three", "first",
    "last", "next", "chapter", "prologue", "epilogue", "part", "book", "god", "lord",
    "sir", "madam", "mister", "missus", "doctor", "captain", "system", "level", "quest",
    "skill", "status", "okay", "oh", "ah", "hey", "yeah", "maybe", "please", "thanks",
    "good", "great", "right", "left", "new", "old", "long", "little", "big",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}

# Honorifics stripped before counting, so "Lord Radiant" and "Radiant" are one candidate.
_TITLES = re.compile(
    r"^(mr|mrs|ms|miss|sir|lady|lord|dame|dr|doctor|professor|captain|commander|"
    r"sergeant|lieutenant|general|king|queen|prince|princess|duke|duchess|father|"
    r"mother|brother|sister|saint|master|mistress)\.?\s+", re.IGNORECASE)

# A candidate must clear this to be worth the model's attention. Below it the pool is
# almost entirely sentence-initial nouns and one-off place names.
MIN_MENTIONS = 3


# Contractions are the biggest source of junk candidates: "I'd", "You're", "Don't" and
# "That's" all begin with a capital and match a naive name pattern. Measured on a real
# book, they occupied four of the top fifteen candidates.
#
# The distinction that matters: a POSSESSIVE folds into its base name ("Sloane's" ->
# "Sloane", the same character), while any other contraction is not a name at all. And a
# genuine apostrophe name (O'Brien, D'Artagnan) has an UPPERCASE letter after the
# apostrophe, which is what separates it from "don't".
_POSSESSIVE = re.compile(r"['’]s\b")
_CONTRACTION = re.compile(r"['’](d|m|s|ll|re|ve|t)\b", re.IGNORECASE)


def _normalise(name: str) -> str:
    name = _TITLES.sub("", name.strip())
    # Fold the possessive into the base name so "Sloane's" is not a second character.
    name = _POSSESSIVE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" '’-")

    # Strip leading sentence furniture. The scanner reads a capitalised run, so a
    # sentence beginning "If Lukas had..." yields the candidate "If Lukas". Measured on a
    # real book this produced "If Lukas", "Plan Sloane" and similar — junk that also
    # *stole mentions* from the real character. Dropping the leading stopword both
    # removes the junk and returns those counts to where they belong.
    words = name.split()
    while len(words) > 1 and words[0].lower() in _STOPWORDS:
        words.pop(0)
    return " ".join(words)


def _is_plausible(name: str) -> bool:
    if len(name) < 2:
        return False
    if _CONTRACTION.search(name):
        return False          # "I'd", "Don't", "You're" — never names
    words = name.split()
    # Every word being a stopword means it is sentence furniture, not a name.
    return not all(w.lower() in _STOPWORDS for w in words)


def harvest(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan chaptered text for name candidates and the evidence about each.

    Returns one record per surface form, with the counts the tiering rules need. No
    model, no network — this runs over a 50k-word book in well under a second.
    """
    mentions: dict[str, int] = defaultdict(int)
    speech: dict[str, int] = defaultdict(int)
    chapters_seen: dict[str, set[int]] = defaultdict(set)
    first_seen: dict[str, int] = {}
    last_seen: dict[str, int] = {}

    for chapter in chapters:
        pos = chapter.get("position", 0)
        text = chapter.get("text", "") or ""

        for match in _CAPITALISED.finditer(text):
            name = _normalise(match.group(1))
            if not _is_plausible(name):
                continue
            mentions[name] += 1
            chapters_seen[name].add(pos)
            first_seen.setdefault(name, pos)
            last_seen[name] = pos

        for pattern in (_SPEAKER_BEFORE, _SPEAKER_AFTER):
            for match in pattern.finditer(text):
                name = _normalise(match.group(1))
                if _is_plausible(name):
                    speech[name] += 1

    out = []
    for name, count in mentions.items():
        if count < MIN_MENTIONS and speech.get(name, 0) == 0:
            continue          # keep any speaker, however rare — silence is the signal
        out.append({
            "name": name,
            "mentions": count,
            "dialogue_hits": speech.get(name, 0),
            "chapters": sorted(chapters_seen[name]),
            "chapter_count": len(chapters_seen[name]),
            "first_chapter": first_seen.get(name, 0),
            "last_chapter": last_seen.get(name, 0),
        })
    out.sort(key=lambda c: (-c["mentions"], c["name"]))
    return out


# --------------------------------------------------------------------------- #
# tiering — evidence in, tier out
# --------------------------------------------------------------------------- #

TIERS = ("primary", "secondary", "filler")

# Chapter spread beats raw mentions: a name in 30 of 40 chapters is structural, while
# 50 mentions inside a single chapter is a set-piece extra.
#
# ORDER MATTERS: tier the CONFIRMED PEOPLE, never the raw lexical pool. Tiering the pool
# is meaningless — measured on a real book it labelled 160 candidates "secondary",
# most of which were game terms and sentence-initial verbs rather than characters. The
# model prunes first; the engine tiers what survives.
PRIMARY_SPREAD = 0.25      # fraction of the book's chapters
SECONDARY_SPREAD = 0.15
PRIMARY_DIALOGUE = 15      # attributed speech acts
SECONDARY_DIALOGUE = 3
SECONDARY_MENTIONS = 15


def assign_tier(candidate: dict[str, Any], total_chapters: int) -> tuple[str, str]:
    """Return `(tier, reason)` for one CONFIRMED character.

    The reason travels with the tier so the UI can show *why* — a tier you cannot
    interrogate is one you cannot correct, and every sheet in pass 2 is built on it.

    Dialogue is weighted heavily on purpose: a character who speaks needs a voice, and a
    voice is most of what a character card is for. A frequently-named character who never
    speaks is usually a title, a place, or someone talked *about*.
    """
    spread = candidate["chapter_count"] / max(total_chapters, 1)
    dialogue = candidate["dialogue_hits"]
    reasons = [f"{candidate['mentions']} mentions",
               f"{candidate['chapter_count']}/{total_chapters} chapters ({spread:.0%})",
               f"{dialogue} speech acts"]

    if spread >= PRIMARY_SPREAD and dialogue >= SECONDARY_DIALOGUE:
        return "primary", "; ".join(reasons + ["wide spread and speaks"])
    if dialogue >= PRIMARY_DIALOGUE:
        return "primary", "; ".join(reasons + ["speaks often"])
    if dialogue >= SECONDARY_DIALOGUE:
        return "secondary", "; ".join(reasons + ["speaks"])
    if spread >= SECONDARY_SPREAD and candidate["mentions"] >= SECONDARY_MENTIONS:
        return "secondary", "; ".join(reasons + ["recurring but silent"])
    return "filler", "; ".join(reasons + ["few mentions, no dialogue"])


def tier_all(candidates: list[dict[str, Any]], total_chapters: int) -> list[dict[str, Any]]:
    out = []
    for c in candidates:
        tier, reason = assign_tier(c, total_chapters)
        out.append(dict(c, tier=tier, tier_reason=reason))
    return out


def containment_pairs(names: list[str]) -> list[tuple[str, str]]:
    """Find `(shorter, longer)` name pairs that are plausibly the same person.

    Resolution runs in batches, so two forms of one character can land in different
    batches and never be compared — measured on a real book, "Lukas" and "Lukas Belmont"
    came out as two separate characters for exactly that reason.

    This is the deterministic half of the fix: a single-token name that appears as a
    whole word inside a longer name is a *candidate* for merging. It does NOT merge them
    — two different people can share a first name, and a wrong merge is far worse than a
    missed one. The model adjudicates the shortlist.
    """
    pairs: list[tuple[str, str]] = []
    for short in names:
        short_tokens = short.split()
        if len(short_tokens) != 1:
            continue
        for long in names:
            if long == short:
                continue
            long_tokens = long.split()
            if len(long_tokens) > 1 and short_tokens[0] in long_tokens:
                pairs.append((short, long))
    return pairs


def summarise(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    by_tier: dict[str, int] = {t: 0 for t in TIERS}
    for c in candidates:
        by_tier[c.get("tier", "filler")] = by_tier.get(c.get("tier", "filler"), 0) + 1
    return {
        "candidates": len(candidates),
        "by_tier": by_tier,
        "speakers": sum(1 for c in candidates if c["dialogue_hits"] > 0),
    }


def context_snippets(chapters: list[dict[str, Any]], name: str,
                     limit: int = 2, width: int = 90) -> list[str]:
    """A couple of very short windows around a name, to help the model judge whether the
    surface form is a person and which other forms it matches.

    Short by design: this is disambiguation context passed transiently to the model, not
    stored content, and the standing rule is transform-never-reproduce.
    """
    out: list[str] = []
    needle = re.compile(rf"\b{re.escape(name)}\b")
    for chapter in chapters:
        text = chapter.get("text", "") or ""
        for m in needle.finditer(text):
            start = max(0, m.start() - width // 2)
            out.append(re.sub(r"\s+", " ", text[start:start + width]).strip())
            if len(out) >= limit:
                return out
    return out
