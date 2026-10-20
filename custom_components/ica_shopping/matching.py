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

from .rules import stem

# Amounts, units and packaging say how much, not what
_PACKAGING = frozenset(
    "g kg hg dl cl ml l msk tsk krm st förp förpackning förpackningar paket burk burkar påse påsar "
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
