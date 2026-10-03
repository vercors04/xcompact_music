"""Normalisation de texte et similarité de chaînes (fonctions pures, testées).

Idée générale : avant de comparer « Beyoncé - Halo (Remastered 2011) » à « beyonce
halo », on ramène les deux à une forme canonique (minuscules, sans accents, sans
ponctuation), puis on sépare le *cœur* du titre de ses *qualificatifs* (ce qui est
entre parenthèses ou après « - »). Les qualificatifs sont classés : neutres
(« Remastered », « feat. X ») ou *variantes* (« Live », « Remix »...), qui changent
l'enregistrement et doivent donc être pénalisés s'ils ne sont pas demandés.

On utilise difflib (bibliothèque standard) plutôt que rapidfuzz : rapidfuzz est
compilé (C++), pénible à installer sous Termux, et pour ~20 candidats par requête
la vitesse de difflib est largement suffisante (< 1 ms).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_TRANSLATE = str.maketrans({
    "’": "'", "‘": "'", "`": "'", "´": "'", "“": '"', "”": '"',
    # Lettres latines que NFKD ne décompose pas (pas d'« accent » séparable) :
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ł": "l", "Ł": "l",
    "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "ı": "i",
})

# Translittération du cyrillique (russe/ukrainien, système courant sans signes) : une
# requête tapée « Kino - Gruppa krovi » doit retrouver « Кино - Группа крови ». Appliquée
# des deux côtés de toute comparaison, après passage en minuscules.
_CYRILLIC = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "є": "ye", "і": "i", "ї": "yi", "ґ": "g",
})
# Tout ce qui n'est ni lettre ni chiffre, *quelle que soit l'écriture* (\w est Unicode en
# Python 3). Avec [^0-9a-z], un titre en cyrillique ou en japonais devenait une chaîne
# vide : deux titres russes différents étaient jugés identiques (similarité 1.0).
_NON_ALNUM = re.compile(r"[\W_]+")


def fold(text: str) -> str:
    """Forme canonique pour comparer : minuscules, sans accents ni ponctuation.

    >>> fold("Beyoncé & JAY-Z — Crazy in Love!")
    'beyonce and jay z crazy in love'
    >>> fold("Кино - Группа крови"), fold("細野晴臣")
    ('kino gruppa krovi', '細野晴臣')
    """
    text = text.translate(_TRANSLATE)
    # Cyrillique d'abord (avant NFKD, qui décomposerait « й » en « и » + brève).
    text = text.casefold().translate(_CYRILLIC)
    # NFKD sépare « é » en « e » + accent combinant, qu'on retire ensuite.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = text.replace("&", " and ").replace("+", " and ")
    text = text.replace("'", "")  # « don't » → « dont » (pas « don t »)
    return _NON_ALNUM.sub(" ", text).strip()


# --------------------------------------------------------------------------- #
# Découpage du titre : cœur + qualificatifs
# --------------------------------------------------------------------------- #

_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
# « Titre - Radio Edit », « Titre – Live » : tiret entouré d'espaces.
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+")
# « Titre feat. X » sans parenthèses.
_FEAT_INLINE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def split_title(title: str) -> tuple[str, list[str]]:
    """Sépare un titre en (cœur, qualificatifs).

    >>> split_title("Around the World (Radio Edit) [2011 Remaster]")
    ('Around the World', ['Radio Edit', '2011 Remaster'])
    >>> split_title("Creep - Acoustic")
    ('Creep', ['Acoustic'])
    """
    qualifiers = [q.strip() for q in _BRACKETS.findall(title) if q.strip()]
    core = _BRACKETS.sub(" ", title)
    parts = _DASH_SUFFIX.split(core)
    core = parts[0]
    qualifiers.extend(p.strip() for p in parts[1:] if p.strip())
    m = _FEAT_INLINE.search(core)
    if m:
        qualifiers.append(m.group(0).strip())
        core = core[: m.start()]
    core = " ".join(core.split())
    if not core:  # titre entièrement entre parenthèses, ex. « (Untitled) »
        return title.strip(), []
    return core, qualifiers


# --------------------------------------------------------------------------- #
# Variantes : mots qui signalent un *autre enregistrement*
# --------------------------------------------------------------------------- #

# Chaque étiquette est associée à des motifs (sur texte déjà passé par fold()).
# \b = frontière de mot, pour que « live » ne déclenche pas sur « oliver ».
VARIANT_PATTERNS: dict[str, str] = {
    "live": r"\blive\b|\bconcert\b|\bsessions?\b|\bunplugged\b|\ben direct\b|\ben public\b",
    "remix": r"\bremix(?:ed)?\b|\brmx\b|\bmix\b|\bbootleg\b|\bflip\b|\brework\b|\bvip\b|\bdub\b",
    "edit": r"\bedit\b|\bsingle version\b|\bradio version\b",
    "acoustic": r"\bacoustic\b|\bacoustique\b|\bunplugged\b|\bstripped\b",
    "instrumental": r"\binstrumental\b|\bkaraoke\b|\bbacking track\b",
    "acapella": r"\ba ?cappella\b|\bacapella\b|\bvocals? only\b",
    "cover": r"\bcover\b|\btribute\b|\breprise de\b|\bin the style of\b|\boriginally performed\b",
    "demo": r"\bdemo\b|\brehearsal\b|\bouttake\b|\bearly version\b|\balternate (?:take|version)\b",
    "speed": r"\bsped up\b|\bspeed up\b|\bnightcore\b|\bslowed\b|\breverb\b|\b8d\b|\bdaycore\b",
    "extended": r"\bextended\b|\bclub mix\b|\b12 inch\b",
    "orchestral": r"\borchestral\b|\bsymphonic\b|\bpiano version\b",
    "reprise": r"\breprise\b",
    "clean": r"\bclean\b|\bcensored\b|\bfriendly\b|\bradio friendly\b",
}
_VARIANT_RE = {tag: re.compile(p) for tag, p in VARIANT_PATTERNS.items()}

# « Remastered », « Mono », « feat. X », « From "Film" »... : neutres. On ne les liste
# pas : tout qualificatif qui n'est pas une variante est neutre. Seules exceptions,
# des expressions neutres qui contiennent un mot de variante : on les efface avant.
_NEUTRAL = re.compile(r"\boriginal (?:mix|version|edit)\b|\balbum (?:version|edit|mix)\b")


def variant_counts(text: str) -> Counter[str]:
    """Nombre d'occurrences de chaque type de variante dans un texte.

    On compte (au lieu d'un simple ensemble) pour distinguer « Live Forever »
    (1 « live », c'est le titre) de « Live Forever (Live at Knebworth) » (2).
    """
    folded = _NEUTRAL.sub(" ", fold(text))
    counts: Counter[str] = Counter()
    for tag, rx in _VARIANT_RE.items():
        n = len(rx.findall(folded))
        if n:
            counts[tag] = n
    return counts


def variant_tags(text: str) -> set[str]:
    """Étiquettes de variante présentes dans un texte (titre complet ou album).

    >>> sorted(variant_tags("Creep (Acoustic Live at the BBC)"))
    ['acoustic', 'live']
    >>> variant_tags("Live Forever")  # le mot est dans le titre lui-même
    {'live'}
    >>> variant_tags("Strobe (Original Mix)")  # « original mix » = la version originale
    set()
    """
    return set(variant_counts(text))


# Nom d'album qui signale un album live. Plus prudent que variant_tags : « Live
# Through This » (Hole) est un album studio, donc pas de « live » en début de nom.
_LIVE_ALBUM = re.compile(r"\blive (?:at|in|from|on)\b|\bunplugged\b|\bin concert\b|\blive$")


def is_live_album(album: str | None) -> bool:
    return bool(album and _LIVE_ALBUM.search(fold(album)))


# --------------------------------------------------------------------------- #
# Artistes
# --------------------------------------------------------------------------- #

_ARTIST_SEP = re.compile(
    r"\s*(?:,|;|/|\s&\s|\sand\s|\set\s|\sx\s|\sfeat\.?\s|\sft\.?\s|\sfeaturing\s|\swith\s|\svs\.?\s)\s*",
    re.IGNORECASE,
)


def split_artists(text: str) -> list[str]:
    """Découpe « A feat. B & C » en ["A", "B", "C"].

    Attention : certains noms contiennent un séparateur (« Simon & Garfunkel »).
    C'est sans conséquence ici car on compare aussi la chaîne complète (voir
    artist_similarity).
    """
    parts = [p.strip() for p in _ARTIST_SEP.split(f" {text} ") if p and p.strip()]
    return parts or [text.strip()]


def _artist_key(name: str) -> str:
    key = fold(name)
    return key[4:] if key.startswith("the ") else key  # « The Beatles » ≈ « Beatles »


_FEAT_PREFIX = re.compile(r"^(?:feat\.?|ft\.?|featuring|with)\s+", re.IGNORECASE)


def featured_artists(title: str) -> list[str]:
    """Artistes invités mentionnés dans un titre.

    >>> featured_artists("Get Lucky (feat. Pharrell Williams and Nile Rodgers)")
    ['Pharrell Williams', 'Nile Rodgers']
    >>> featured_artists("Creep (Acoustic)")
    []
    """
    out: list[str] = []
    for q in split_title(title)[1]:
        if _FEAT_PREFIX.match(q):
            out += split_artists(_FEAT_PREFIX.sub("", q))
    return out


def unrequested_guests(query_artist: str, query_title: str, names: list[str]) -> bool:
    """Parmi `names`, y a-t-il un artiste que la requête ne mentionne pas ?

    Un nom est « mentionné » s'il est proche (ratio ≥ 0,8) ou inclus dans un nom demandé
    (« Pharrell » ↔ « Pharrell Williams »).

    >>> unrequested_guests("Stromae", "Alors on danse", ["Stromae", "Kanye West"])
    True
    >>> unrequested_guests("Daft Punk feat. Pharrell", "Get Lucky", ["Daft Punk", "Pharrell Williams"])
    False
    """
    asked = [_artist_key(a) for a in [*split_artists(query_artist), *featured_artists(query_title), query_artist]]
    asked = [a for a in asked if a]
    present = [_artist_key(p) for a in names for p in split_artists(a)]

    def mentioned(name: str) -> bool:
        return any(ratio(name, a) >= 0.8 or (min(len(name), len(a)) >= 3 and (name in a or a in name))
                   for a in asked)

    return any(p and not mentioned(p) for p in present)


# Qualificatifs qu'on sait neutres (même enregistrement) : remaster, année, mono/stéréo,
# version album, « feat. X », « From "Film" »… Tout autre qualificatif sans mot de
# variante est « inconnu » (« Angel Dust » est en réalité un remix).
_KNOWN_NEUTRAL = re.compile(
    r"\bremaster(?:ed|ing)?\b|\b(?:19|20)\d\d\b|\bmono\b|\bstereo\b|\bdeluxe\b|\bbonus\b|\bexplicit\b"
    r"|\balbum version\b|\boriginal (?:mix|version)\b|^(?:feat|ft|featuring|with|from)\b"
)


def has_unknown_qualifier(title: str) -> bool:
    """Le titre porte-t-il un qualificatif qu'on ne sait pas interpréter ?

    >>> has_unknown_qualifier("Angel (Angel Dust)"), has_unknown_qualifier("Hey Jude (2015 Remaster)")
    (True, False)
    >>> has_unknown_qualifier("Alors on danse (Radio Edit)"), has_unknown_qualifier("Angel (feat. Horace Andy)")
    (False, False)
    """
    for q in split_title(title)[1]:
        folded = fold(q)
        if not variant_counts(q) and not _KNOWN_NEUTRAL.search(folded):
            return True
    return False


# --------------------------------------------------------------------------- #
# Similarité
# --------------------------------------------------------------------------- #


def ratio(a: str, b: str) -> float:
    """Similarité de séquence (0 → 1) : 2 × (caractères communs) / (longueurs)."""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def similarity(a: str, b: str) -> float:
    """Similarité robuste à l'ordre des mots.

    max(ratio direct, ratio des mots triés). On n'utilise PAS le « token set ratio »
    de fuzzywuzzy : il donne 1.0 dès qu'une chaîne est incluse dans l'autre
    (« love » vs « love me do »), ce qui serait catastrophique pour des titres.

    >>> round(similarity("around the world", "world around the"), 2)
    1.0
    >>> similarity("love", "love me do") < 0.6
    True
    """
    raw_a, raw_b = a, b
    a, b = fold(a), fold(b)
    if not a and not b:  # que de la ponctuation (le groupe « !!! ») : on compare tel quel
        return 1.0 if raw_a.strip().casefold() == raw_b.strip().casefold() else 0.0
    direct = ratio(a, b)
    sorted_tokens = ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split())))
    return max(direct, sorted_tokens)


def artist_similarity(query_artist: str, candidate_artists: list[str]) -> float:
    """Proximité entre l'artiste demandé et la liste d'artistes d'un candidat.

    Chaque artiste demandé est cherché parmi ceux du candidat (meilleure paire) et on
    moyenne. Un candidat qui a *plus* d'artistes (featurings) n'est pas pénalisé :
    demander « Daft Punk » et trouver « Daft Punk, Pharrell Williams » vaut 1.0.

    >>> artist_similarity("Daft Punk", ["Daft Punk", "Pharrell Williams"])
    1.0
    >>> artist_similarity("The Beatles", ["Beatles"])
    1.0
    """
    if not candidate_artists:
        return 0.0
    cand_keys = [_artist_key(c) for p in candidate_artists for c in split_artists(p)]
    cand_keys += [_artist_key(c) for c in candidate_artists]  # noms entiers aussi
    wanted = [_artist_key(q) for q in split_artists(query_artist)]
    per_artist = sum(max(ratio(w, c) for c in cand_keys) for w in wanted) / len(wanted)
    whole = ratio(_artist_key(query_artist), _artist_key(" ".join(candidate_artists)))
    return max(per_artist, whole)
