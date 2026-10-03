"""Lecture des requêtes : arguments, fichier texte, entrée standard."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

from musique.models import Query
from musique.textnorm import fold

# Séparateur artiste/titre : un tiret (ou demi-cadratin/cadratin) entouré d'espaces.
# « AC-DC » ou « Jay-Z » ne sont donc pas coupés.
_SEP = re.compile(r"\s+[-–—]\s+")


def parse_query(line: str) -> Query | None:
    """« Artiste - Titre » → Query(artist, title) ; texte libre → Query(raw).

    Les lignes vides et les commentaires (#) donnent None.

    >>> parse_query("Daft Punk - Around the World")
    Query(raw='Daft Punk - Around the World', artist='Daft Punk', title='Around the World', duration=None, album=None)
    >>> parse_query("radiohead creep").artist is None
    True
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = _SEP.split(line, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return Query(raw=line, artist=parts[0].strip(), title=parts[1].strip())
    return Query(raw=line)


def query_key(q: Query) -> str:
    """Clé stable d'une requête (pour le cache et la déduplication)."""
    if q.artist and q.title:
        return f"{fold(q.artist)}|{fold(q.title)}"
    return fold(q.raw)


def read_lines(path: str | Path) -> list[str]:
    """Lit un fichier de requêtes, une par ligne.

    `utf-8-sig` absorbe le BOM qu'ajoute parfois le Bloc-notes Windows ; le mode
    texte de Python gère indifféremment les fins de ligne \\n et \\r\\n.
    """
    if str(path) == "-":
        return sys.stdin.read().splitlines()
    return Path(path).read_text(encoding="utf-8-sig").splitlines()


def collect(lines: Iterable[str]) -> list[Query]:
    """Parse et déduplique (en gardant l'ordre) une suite de lignes."""
    seen: set[str] = set()
    out: list[Query] = []
    for line in lines:
        q = parse_query(line)
        if q is None:
            continue
        k = query_key(q)
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out
