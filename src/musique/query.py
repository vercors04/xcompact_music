"""Lecture des requêtes : arguments, fichier texte, entrée standard."""

from __future__ import annotations

import csv
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

    Un lien de vidéo YouTube donne une requête « directe » (pas de recherche).

    >>> parse_query("Daft Punk - Around the World")
    Query(raw='Daft Punk - Around the World', artist='Daft Punk', title='Around the World', duration=None, album=None, url=None)
    >>> parse_query("radiohead creep").artist is None
    True
    >>> parse_query("https://youtu.be/yzrz5u_q_e8?si=x").url
    'https://www.youtube.com/watch?v=yzrz5u_q_e8'
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    vid = video_id(line)
    if vid:
        return Query(raw=line, url=f"https://www.youtube.com/watch?v={vid}")
    parts = _SEP.split(line, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return Query(raw=line, artist=parts[0].strip(), title=parts[1].strip())
    return Query(raw=line)


_YT_VIDEO = re.compile(
    r"^https?://(?:(?:www|m|music)\.)?(?:youtube\.com/(?:watch\?(?:\S*&)?v=|shorts/)|youtu\.be/)([\w-]{11})(?:[?&#]\S*)?$")


def video_id(text: str) -> str | None:
    """Identifiant de vidéo d'un lien YouTube / YouTube Music, sinon None.

    >>> video_id("https://music.youtube.com/watch?v=Jb6gcoR266U&list=RDAMVM"), video_id("Daft Punk - Da Funk")
    ('Jb6gcoR266U', None)
    """
    m = _YT_VIDEO.match(text.strip())
    return m.group(1) if m else None


def query_key(q: Query) -> str:
    """Clé stable d'une requête (pour le cache et la déduplication)."""
    if q.url:
        return f"url:{video_id(q.url)}"
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
    return dedupe(q for q in map(parse_query, lines) if q is not None)


def dedupe(queries: Iterable[Query]) -> list[Query]:
    """Retire les doublons (même clé normalisée) en gardant le premier et l'ordre."""
    seen: set[str] = set()
    out: list[Query] = []
    for q in queries:
        k = query_key(q)
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out


# Colonnes reconnues dans un export CSV de playlist Spotify : celui d'Exportify
# (« Track Name », « Artist Name(s) », « Duration (ms) ») et celui de sites comme
# Chosic / Skiley (« Song », « Artist », « Duration » en mm:ss).
_CSV_TITLE = ("Track Name", "Song", "Title", "Titre")
_CSV_ARTIST = ("Artist Name(s)", "Artist", "Artists", "Artiste")
_CSV_ALBUM = ("Album Name", "Album")


def _csv_duration(row: dict) -> float | None:
    """Durée en secondes : « Duration (ms) » = 247000, ou « Duration » = « 04:07 »."""
    ms = (row.get("Duration (ms)") or "").strip()
    if ms.isdigit():
        return int(ms) / 1000
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d{2})", (row.get("Duration") or "").strip())
    if m:
        return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])
    return None


def read_csv(path: str | Path) -> list[Query]:
    """Requêtes depuis un export CSV de playlist (Spotify via Exportify, Chosic…).

    Avantage sur un fichier texte : la durée de chaque morceau est connue, ce qui
    départage studio / live / radio edit (voir matching.duration_factor). Les artistes
    « A,B » deviennent « A, B ».

    >>> row = {"Song": "Colors", "Artist": "Black Pumas", "Duration": "04:07", "Album": "Black Pumas"}
    >>> _csv_query(row)
    Query(raw='Black Pumas - Colors', artist='Black Pumas', title='Colors', duration=247, album='Black Pumas', url=None)
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not any(k in rows[0] for k in _CSV_TITLE):
        raise ValueError(f"colonne de titre introuvable (attendu : {', '.join(_CSV_TITLE)})")
    return dedupe(q for q in map(_csv_query, rows) if q is not None)


def _csv_query(row: dict) -> Query | None:
    def first(keys: tuple[str, ...]) -> str:
        return next(((row.get(k) or "").strip() for k in keys if (row.get(k) or "").strip()), "")

    title = first(_CSV_TITLE)
    artist = ", ".join(a.strip() for a in first(_CSV_ARTIST).split(",") if a.strip())
    if not title:
        return None
    if not artist:
        return Query(raw=title, duration=_csv_duration(row))
    return Query(raw=f"{artist} - {title}", artist=artist, title=title, duration=_csv_duration(row),
                 album=first(_CSV_ALBUM) or None)
