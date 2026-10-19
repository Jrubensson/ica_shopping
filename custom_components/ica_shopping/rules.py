"""Food rules: ingredients the household avoids, and its own versions of ICA recipes.

A rule has one of two levels:

- never ("Aldrig"): recipes with the ingredient are not suggested at all.
- omit ("Hellre utan"): recipes built around the ingredient are not suggested (Löksås och
  fläsk for "lök"); in the rest it is simply left out, and off the shopping list.

A dish counts as built around an ingredient when its title names it (Pannbiff med löksås), or
when ICA tags the recipe with it as its protein or kind of dish (Langos med skagenröra is
tagged "Protein": "Räkor"). Dishes the user names themselves are always planned; the rules
then only leave out what isn't the point.
"""
from __future__ import annotations

import re
from typing import Iterable

LEVEL_NEVER = "never"
LEVEL_OMIT = "omit"
LEVELS = (LEVEL_NEVER, LEVEL_OMIT)

# ICA tag kinds that say what a dish is built around. The vegetable, dairy and pantry tags
# don't: richly tagged recipes list nearly every ingredient there (Klassisk lasagne is tagged
# "Lök" for its two onions), and the rest are meal, occasion, diet and so on.
THEME_CATEGORIES = frozenset({"Protein", "Styckdel", "Typ av recept"})

# Plural and definite endings, so "räkor" also finds "räkpasta" and "lökar" finds "lök".
_ENDINGS = ("orna", "arna", "erna", "or", "ar", "er")


def fold(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def stem(word: str) -> str:
    word = fold(word)
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def make_rule(
    ingredient: str,
    level: str = LEVEL_OMIT,
    terms: Iterable[str] | None = None,
    excepts: Iterable[str] | None = None,
) -> dict:
    """A rule as stored. `terms` default to the ingredient's stem; `excepts` are words that
    contain a term but are something else ("vitlök" and "gräslök" for "lök")."""
    name = (ingredient or "").strip()
    return {
        "ingredient": name,
        "level": level if level in LEVELS else LEVEL_OMIT,
        "terms": [t for t in dict.fromkeys(fold(t) for t in (terms or [stem(name)])) if t],
        "except": [e for e in dict.fromkeys(fold(e) for e in excepts or ()) if e],
    }


def mentions(rule: dict, text) -> bool:
    """Whether `text` (an ingredient line, a title, a category) names the rule's ingredient."""
    folded = fold(text)
    for word in rule.get("except") or ():
        folded = folded.replace(word, " ")
    return any(term in folded for term in rule.get("terms") or ())


def is_theme(rule: dict, title, categories: Iterable[dict] = ()) -> bool:
    """Whether a dish is built around the rule's ingredient: its title, or ICA's tags, name it."""
    if mentions(rule, title):
        return True
    return any(
        isinstance(c, dict) and c.get("name") in THEME_CATEGORIES and mentions(rule, c.get("value"))
        for c in categories
    )


def touched_rules(rules: Iterable[dict], title, lines: Iterable[str]) -> list[dict]:
    """The rules whose ingredient is in the dish at all."""
    lines = list(lines)
    return [r for r in rules if mentions(r, title) or any(mentions(r, line) for line in lines)]


def adjust(
    lines: Iterable[str],
    rules: Iterable[dict],
    variant: dict | None = None,
    keep: Iterable[str] = (),
) -> tuple[list[dict], list[str]]:
    """(omitted, extra) for one dinner.

    omitted is [{"line", "rule"}]: lines left out by a rule (its ingredient name in "rule"), or
    by the household's own version of the recipe (rule None). extra is the lines that version
    adds. Rules named in `keep` are ones the user overrode by asking for the dish.
    """
    kept = {fold(k) for k in keep}
    active = [r for r in rules if fold(r.get("ingredient")) not in kept]
    own = {fold(line) for line in (variant or {}).get("omit") or ()}
    omitted = []
    for line in lines:
        if fold(line) in own:
            omitted.append({"line": line, "rule": None})
        elif rule := next((r for r in active if mentions(r, line)), None):
            omitted.append({"line": line, "rule": rule["ingredient"]})
    return omitted, list((variant or {}).get("add") or ())
