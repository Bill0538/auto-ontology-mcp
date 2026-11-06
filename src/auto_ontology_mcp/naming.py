"""Name and text helpers shared by inference, modeling and search."""

from __future__ import annotations

import re
import unicodedata

_CAMEL_1 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_2 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^0-9A-Za-zÀ-￿]+")
_TOKEN = re.compile(r"[0-9A-Za-zÀ-￿]+")

# Suffixes that mark identifier-like columns (customer_id, CustomerID, order_no …).
_ID_SUFFIXES = ("id", "key", "code", "no", "num", "number", "ref", "uuid", "guid")

STOPWORDS = frozenset(
    """
    a an the of for to in on at by with from and or per vs versus is are was were be been
    what which who whom whose when where why how many much does do did show list give get
    find tell me my our their his her its this that these those all any each every
    please can could would should will there than then into over under between during
    last next previous current first top bottom most least total number count sum avg
    average min max minimum maximum
    """.split()
)


def split_words(name: str) -> list[str]:
    """Split snake_case / camelCase / PascalCase / spaced names into words."""
    separated = _CAMEL_1.sub(" ", name.strip())
    separated = _CAMEL_2.sub(" ", separated)
    return [p for p in _SPLIT.split(separated) if p]


def to_label(name: str) -> str:
    """Human label with spaces between words (``purchase_orders`` → ``Purchase Orders``).

    Mirrors Auto Ontology's ``to_term_name``: acronyms stay upper-case.
    """
    return " ".join(p if p.isupper() and len(p) > 1 else p.capitalize() for p in split_words(name))


def singular(word: str) -> str:
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes", "zes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def compact(name: str) -> str:
    """Lower-case, separators removed: ``Delivery_City_ID`` → ``deliverycityid``."""
    return "".join(p.lower() for p in split_words(name))


def compact_singular(name: str) -> str:
    words = split_words(name)
    if not words:
        return ""
    words = [w.lower() for w in words]
    words[-1] = singular(words[-1])
    return "".join(words)


def id_base(column: str) -> str | None:
    """Return the entity part of an identifier-like column, or None.

    ``customer_id`` → ``customer``; ``DeliveryCityID`` → ``deliverycity``;
    ``order_no`` → ``order``; ``id`` → ``""``.
    """
    words = [w.lower() for w in split_words(column)]
    if not words:
        return None
    if words[-1] in _ID_SUFFIXES:
        return "".join(words[:-1])
    low = column.lower()
    # Unseparated lowercase forms such as ``customerid``.
    for suffix in ("id", "key", "uuid"):
        if low.endswith(suffix) and len(low) > len(suffix) + 2 and low.isalnum():
            return low[: -len(suffix)]
    return None


def is_id_like(column: str) -> bool:
    return id_base(column) is not None


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def tokens(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """Search tokens: split camel/snake case, lowercase, singularize."""
    out: list[str] = []
    for raw in _TOKEN.findall(unicodedata.normalize("NFKC", text or "")):
        for word in split_words(raw) or [raw]:
            w = word.casefold()
            if drop_stopwords and w in STOPWORDS:
                continue
            out.append(singular(w) if w.isascii() else w)
    return out


def trigrams(text: str) -> set[str]:
    s = f"  {normalize_text(text)} "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


LABEL_RE = re.compile(r"^[^\s_].*[^\s_]$|^[^\s_]$")


def clean_label(name: str) -> str:
    """Normalize a proposed Term / attribute label to spaced words.

    ``PurchaseOrder`` and ``purchase_order`` become ``Purchase Order``; labels
    that already contain spaces (or non-ASCII text such as Korean) are kept,
    with whitespace collapsed.
    """
    name = " ".join((name or "").split())
    if not name:
        return name
    if " " in name or not name.isascii():
        return name
    return to_label(name)
