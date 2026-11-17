"""Bad-word finder used by automod. Handles leetspeak / masking (fu#k, a$$, sh!t, f**k, b1tch, fuuuck, f.u.c.k),
accents and full-width letters. Pure Python, no Discord imports."""
from __future__ import annotations

import random
import re
import unicodedata
from functools import lru_cache

NICE_WORDS = [
    "acche", "sundar", "pyaare", "pyaara", "pyaar", "meetha", "khush", "dost", "phool", "muskaan",
    "love", "peace", "sweet", "smile", "sunshine", "angel", "hug", "rainbow", "cutie", "happy",
]

# Strong words: matched even inside a bigger word ("fucking", "motherfucker").
SUB_WORDS = [
    "fuck", "fuk", "fck", "bitch", "biatch", "bastard", "pussy", "whore", "slut", "nigga", "nigger",
    "niga", "faggot", "fagot", "asshole", "dumbass", "jackass", "fatass", "bullshit", "horseshit", "dipshit",
    "wanker", "twat", "bollock", "dildo", "blowjob", "handjob", "cocksucker", "dickhead", "retard",
    "madarchod", "maderchod", "madrchod", "behenchod", "bhenchod", "benchod", "behnchod", "bhosdike", "bhosdika",
    "bhosd", "bhosada", "chutiya", "chutia", "chutmar", "gandu", "gaandu", "gaand", "bakchod", "lauda", "lavda",
    "lawda", "lodu", "haramkhor", "haramzada", "kaminey", "bhadwa", "bhadwe", "randwa",
]
# Short / risky words: only as a whole word (so "class", "assume", "chutney" stay untouched).
WORD_WORDS = [
    "ass", "cunt", "dick", "cock", "shit", "piss", "sala", "saala", "saale", "mc", "bc", "mf", "bkl", "mkc", "bsdk",
    "chut", "gand", "lund", "randi", "raand", "rand", "harami", "kamina", "kamine", "dox", "doxx",
]

LEET = {
    "a": "a4@", "b": "b8", "e": "e3", "g": "g96", "i": "i1!|l", "l": "l1|i", "o": "o0", "s": "s5$z",
    "t": "t7+", "u": "uv", "z": "z2s",
}
MASK = "*#@$%&!?^~"
SUFFIX = r"(?:s|es|ed|er|ers|ing|in|y|z)?"
ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"}
PROTECT = re.compile(r"<[^>\s]+>|https?://\S+")  # mentions, emojis, links are never touched


def _piece(ch: str, first: bool, masks: bool = True) -> str:
    cls = "".join(re.escape(c) for c in LEET.get(ch, ch))
    if first or not masks:
        return f"[{cls}]{{1,2}}"
    return f"(?:[{cls}]{{1,2}}|[{re.escape(MASK)}]+)"


def build_pattern(word: str, whole: bool) -> str:
    word = re.sub(r"[^a-z0-9]", "", word.lower())
    pieces = [_piece(ch, i == 0, masks=len(word) > 2) for i, ch in enumerate(word)]  # "mc"/"bc" never use masks
    body = r"[._\-]?".join(pieces)
    if whole:
        return rf"(?<![a-z0-9]){body}{SUFFIX}(?![a-z0-9])"
    return body


def _compile(sub: tuple[str, ...], whole: tuple[str, ...]) -> re.Pattern | None:
    patterns = [build_pattern(w, False) for w in sub if w] + [build_pattern(w, True) for w in whole if w]
    return re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None


_BUILTIN = _compile(tuple(SUB_WORDS), tuple(WORD_WORDS))


@lru_cache(maxsize=256)
def _custom(words: tuple[str, ...]) -> re.Pattern | None:
    return _compile((), words)


def normalize(text: str) -> tuple[str, list[int]]:
    """Lowercase ascii-ish text and, for every char, the index it came from in `text`.
    Runs of the same letter/digit are cut to 2 ("fuuuuck" -> "fuuck")."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch in ZERO_WIDTH:
            continue
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            c = c.lower()
            if len(out) >= 2 and c.isalnum() and out[-1] == c and out[-2] == c:
                continue
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def find_spans(text: str, extra_words: tuple[str, ...] = ()) -> list[tuple[int, int]]:
    if not text:
        return []
    norm, idx = normalize(text)
    protected = [(m.start(), m.end()) for m in PROTECT.finditer(text)]
    spans: list[tuple[int, int]] = []
    for rx in (_BUILTIN, _custom(tuple(w.lower() for w in extra_words))):
        if rx is None:
            continue
        for m in rx.finditer(norm):
            if m.end() == m.start():
                continue
            chunk = norm[m.start():m.end()]
            letters = sum(c.isalnum() for c in chunk)
            if letters < 2 and len(chunk) - letters > 2:  # "t?!!!" is not "twat"
                continue
            start, end = idx[m.start()], idx[m.end() - 1] + 1
            if any(start < pe and end > ps for ps, pe in protected):
                continue
            while start > 0 and text[start - 1].isalnum():
                start -= 1
            while end < len(text) and text[end].isalnum():
                end += 1
            spans.append((start, end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def clean_text(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace every span with a random nice word (keeps UPPER / Capital style)."""
    result, last = [], 0
    for s, e in spans:
        original = text[s:e]
        word = random.choice(NICE_WORDS)
        if len(original) > 1 and original.isupper():
            word = word.upper()
        elif original[:1].isupper():
            word = word.capitalize()
        result.append(text[last:s] + word)
        last = e
    result.append(text[last:])
    return "".join(result)


def contains_bad(text: str, extra_words: tuple[str, ...] = ()) -> bool:
    return bool(find_spans(text, extra_words))
