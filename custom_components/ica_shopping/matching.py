"""Whether a shopping list row already covers a recipe's ingredient line.

ICA's lines and the rows people keep rarely read the same: "8 varmkorvar" against "Varmkorv",
"2 dl mjölk (gärna 1,5 %)" against "Mjölk", "4 bitar mjukt tunnbröd" against "Mjukt
tunnbröd". A row covers a line when every word of the row, amounts and packaging left out, is
in the line: "Mjukt tunnbröd" covers the tunnbröd, but "Mjölig potatis" not "900 g fast
potatis". A row that states an amount only covers a line with the same amounts, so a second
plan for "4.5 dl" gets its own row next to one for "3 dl". How it is bought doesn't count
against a row ("Frysta ärtor" covers "250 g ärtor"), and a line for a processed kind
("krossade tomater") isn't covered by the plain thing.
"""
from __future__ import annotations

import re

from .rules import fold, stem

# Amounts, units and packaging say how much, not what
_PACKAGING = frozenset(
    "g kg hg dl cl ml l liter msk tsk krm st förp förpackning förpackningar paket burk burkar påse påsar "
    "kruka krukor bit bitar knippe knippen skiva skivor klyfta klyftor port portion portioner "
    "à ca eller och med till".split()
)
# Made into something else; a row for the plain thing doesn't stand in for these
_PROCESSED = frozenset(
    "krossade krossad passerade passerad torkad torkade soltorkade rökt rökta konserverad "
    "konserverade inlagd inlagda pulver".split()
)

# How the thing is bought; a row may say so where the recipe doesn't
_BOUGHT_AS = frozenset("fryst frysta djupfryst djupfrysta färsk färska eko ekologisk ekologiska svensk svenska".split())


def _key(word: str) -> str:
    """Stem without plural, definite and final vowel endings: "gurka" and "gurkor" alike."""
    word = stem(word)
    return word[:-1] if len(word) >= 4 and word[-1] in "ae" else word


def words(text: str) -> set[str]:
    """What a row or line is about: its words, with brackets, amounts and packaging left out."""
    text = re.sub(r"\([^)]*\)", " ", (text or "").lower())
    return {
        _key(word)
        for word in re.findall(r"[a-zåäöéèüàç]+", text)
        if word not in _PACKAGING
    }


_PROCESSED_KEYS = frozenset(_key(w) for w in _PROCESSED)
_BOUGHT_AS_KEYS = frozenset(_key(w) for w in _BOUGHT_AS)


def _amounts(text: str) -> list[str]:
    return [n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", text or "")]


def covers(row: str, line: str) -> bool:
    """Whether the list row `row` already has what the ingredient line `line` asks for."""
    if (amounts := _amounts(row)) and amounts != _amounts(line):
        return False
    need = words(line)
    have = words(row)
    have = (have - _BOUGHT_AS_KEYS) or have
    if not have or not have <= need:
        return False
    return not any(word in _PROCESSED_KEYS for word in need - have)


def grocery_names(text: str) -> list[str]:
    """Names to look a row up by in ICA's table of groceries, the most specific first. The
    amount and packaging come off ("Potatis 7 st" is potatis), only the first of alternatives
    counts ("sej eller torsk 400 g" is sej), and words describing the grocery fall away from
    the front ("riven ost 1,5 dl" is riven ost, then ost)."""
    whole = fold(text)
    text = re.sub(r"\([^)]*\)", " ", whole)
    text = re.split(r"\b(?:eller|med|till)\b|,(?!\d)", text)[0]
    found = [word for word in re.findall(r"[a-zåäöéèüàç]+", text) if word not in _PACKAGING]
    names = [whole] + [" ".join(found[i:]) for i in range(len(found))]
    return list(dict.fromkeys(name for name in names if name))


# What one of each unit is, in the unit amounts of its kind add up in
_UNIT_SIZES = {
    "ml": ("l", 0.001), "cl": ("l", 0.01), "dl": ("l", 0.1), "l": ("l", 1.0), "liter": ("l", 1.0),
    "g": ("kg", 0.001), "hg": ("kg", 0.1), "kg": ("kg", 1.0),
    "st": ("st", 1.0), "": ("st", 1.0),
}
_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)\s*(%|[a-zåäö]+)?")


def _quantity(text: str) -> tuple[float, str, float, re.Match] | bool | None:
    """(number, kind, size of its unit, where it is) of a row's one amount: "Potatis 7 st" is
    7 st, "3 potatisar" 3 st, "Mjölk 1,5 l" 1.5 l. None without an amount, and False when the
    row says more than one ("2 x 500 g") or in brackets, which aren't added up."""
    if "(" in text:
        return False if re.search(r"\d", text) else None
    found = [m for m in _NUMBER.finditer(text.lower()) if m.group(2) != "%"]
    if not found:
        return None
    if len(found) > 1:
        return False
    match = found[0]
    unit = match.group(2) or ""
    if unit in _UNIT_SIZES:
        kind, size = _UNIT_SIZES[unit]
    elif unit in _PACKAGING:
        kind, size = _key(unit), 1.0
    else:
        # "3 potatisar": the word is the grocery
        kind, size = "st", 1.0
    return float(match.group(1).replace(",", ".")), kind, size, match


def _what(text: str) -> frozenset[str]:
    """What a row is, its amount left out: its words, also in brackets, and any percentage
    ("Mjölk 3%" isn't "Mjölk 1,5%")."""
    percent = {p.replace(",", ".").replace(" ", "") for p in re.findall(r"\d+(?:[.,]\d+)?\s*%", text or "")}
    return frozenset(words(re.sub(r"[()]", " ", text or "")) | percent)


def same(row: str, item: str) -> bool:
    """Whether two rows are the same thing, whatever their amounts."""
    return bool(_what(row)) and _what(row) == _what(item)


def merge(row: str, item: str) -> str | None:
    """The row once `item` is added to it, when the two are the same thing: "Mjölk" with
    "Mjölk 1 l" is "Mjölk 1 l", "Potatis 7 st" with "3 potatisar" "Potatis 10 st", and "Mjölk
    1 l" with "Mjölk" or "1 l mjölk" stays as it is. None when they are different things or
    the amounts don't add up ("Potatis 2 kg" and "Potatis 7 st")."""
    if not same(row, item):
        return None
    have, more = _quantity(row), _quantity(item)
    if more is None:
        return row
    if have is None:
        return item
    if have is False or more is False or have[1] != more[1]:
        return None
    value, _, size, match = have
    if abs(more[0] * more[2] - value * size) < 1e-9:
        # The same amount again is the row said again, not twice as much
        return row
    total = value + more[0] * more[2] / size
    number = f"{total:.3f}".rstrip("0").rstrip(".")
    if "," in match.group(1) or "," in more[3].group(1):
        number = number.replace(".", ",")
    return row[: match.start(1)] + number + row[match.end(1):]
