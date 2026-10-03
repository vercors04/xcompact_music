"""Bibliothèque : nommage portable, écriture atomique, dossier de travail, index, verrou.

Règles de nommage : on applique partout les règles les plus strictes (Windows/FAT32),
car la carte SD du téléphone est en FAT32 (vérifié : « : » y est refusé, la casse est
ignorée) et un nom accepté sur un appareil doit l'être sur tous ceux que Syncthing
synchronise.

État de la bibliothèque : il n'y a PAS de fichier d'archive partagé (style
yt-dlp --download-archive). La provenance de chaque fichier est écrite dans ses tags
(MUSIQUE_SOURCE) ; l'index n'est qu'un cache local à l'appareil, reconstructible en
rescannant la bibliothèque. Aucun fichier modifiable n'est donc partagé entre
appareils : pas de conflit Syncthing possible sur l'état.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from musique.models import TrackMeta
from musique.textnorm import fold

AUDIO_EXTS = {".opus", ".ogg", ".m4a", ".mp3", ".flac"}
WORK_DIRNAME = ".musique"  # dans la bibliothèque : même système de fichiers → rename atomique

# --------------------------------------------------------------------------- #
# Nommage
# --------------------------------------------------------------------------- #

_FORBIDDEN = {
    "/": "-", "\\": "-", ":": " -", "|": "-", "?": "", "*": "", '"': "'", "<": "", ">": "",
}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_component(name: str, max_bytes: int = 150) -> str:
    """Rend un nom de dossier/fichier valide sous Windows, FAT32, Android et Linux.

    * NFC : « é » toujours en un seul point de code (sinon deux noms visuellement
      identiques peuvent coexister et perturber Syncthing).
    * caractères interdits remplacés, caractères de contrôle supprimés.
    * pas de point/espace final (Windows les supprime silencieusement), pas de point
      initial (fichier caché, et Android ignorerait le dossier).
    * noms réservés Windows (CON, NUL...) suffixés.
    * longueur limitée en *octets UTF-8* (ext4 : 255 octets ; on garde de la marge
      pour le chemin complet et l'extension).

    >>> sanitize_component("AC/DC: Live?")
    'AC-DC - Live'
    >>> sanitize_component("con")
    'con_'
    """
    s = unicodedata.normalize("NFC", name)
    s = "".join(_FORBIDDEN.get(c, c) for c in s if ord(c) >= 32)
    s = " ".join(s.split())
    s = s.strip(" .")
    while s.startswith("."):
        s = s[1:].lstrip()
    if s.split(".")[0].upper() in _RESERVED:
        s += "_"
    encoded = s.encode("utf-8")
    if len(encoded) > max_bytes:
        s = encoded[:max_bytes].decode("utf-8", "ignore").rstrip(" .")
    return s or "_"


LAYOUTS = ("flat", "artist_album")


def track_relpath(meta: TrackMeta, ext: str, layout: str = "flat") -> Path:
    """Chemin du fichier, relatif à la bibliothèque.

    * "flat" (défaut) : tout à la racine, « Artiste - Titre.ext ». Tu ranges ensuite
      comme tu veux : l'outil retrouve les fichiers déplacés grâce à leurs tags.
    * "artist_album" : « Artiste de l'album/Album/NN - Titre.ext ». On range par
      *artiste de l'album* : sinon les featurings éclateraient un album en plusieurs
      dossiers.

    >>> track_relpath(TrackMeta("Get Lucky", ["Daft Punk", "Pharrell Williams"]), ".opus").as_posix()
    'Daft Punk, Pharrell Williams - Get Lucky.opus'
    """
    if layout == "flat":
        name = sanitize_component(f"{meta.artist_display or 'Artiste inconnu'} - {meta.title}", max_bytes=180)
        return Path(f"{name}{ext}")
    if layout != "artist_album":
        raise ValueError(f"disposition inconnue : {layout!r} (possibles : {', '.join(LAYOUTS)})")
    artist = sanitize_component(meta.album_artist_display or meta.artist_display or "Artiste inconnu")
    album = sanitize_component(meta.album) if meta.album else "[Sans album]"
    title = sanitize_component(meta.title, max_bytes=120)
    if meta.track_number:
        disc = f"{meta.disc_number}-" if meta.disc_number and meta.disc_number > 1 else ""
        name = f"{disc}{meta.track_number:02d} - {title}"
    else:
        name = title
    return Path(artist) / album / f"{name}{ext}"


def _same_name(a: str, b: str) -> bool:
    return unicodedata.normalize("NFC", a).casefold() == unicodedata.normalize("NFC", b).casefold()


def resolve_existing(root: Path, rel: Path) -> Path:
    """Chemin réel à utiliser pour `rel`, en réutilisant les dossiers déjà présents
    même si leur casse diffère (« Daft punk/ » existant → réutilisé pour « Daft Punk »).

    Nécessaire pour un comportement identique sur Linux (ext4, sensible à la casse)
    et sur FAT32/Windows (insensibles) : sinon « Daft Punk/ » et « Daft punk/ »
    coexisteraient sur l'un et entreraient en collision sur l'autre via Syncthing.
    """
    current = root
    for part in rel.parts:
        match = None
        if current.is_dir():
            match = next((e for e in os.listdir(current) if _same_name(e, part)), None)
        current = current / (match or part)
    return current


def place_atomically(src: Path, dest: Path, retries: int = 3) -> Path:
    """Déplace `src` (dans .musique/tmp) vers `dest` en une seule opération.

    `os.replace` est atomique sur un même système de fichiers : un lecteur ou
    Syncthing voit soit rien, soit le fichier complet, jamais un fichier à moitié
    écrit. C'est pour ça que le dossier temporaire est DANS la bibliothèque : un
    dossier temporaire dans le home de Termux serait sur un autre système de
    fichiers, et le « déplacement » deviendrait une copie non atomique.
    Sous Windows, os.replace échoue si un programme a le fichier ouvert : on réessaie.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(dest)
    for attempt in range(retries):
        try:
            os.replace(src, dest)
            return dest
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(1.0 + attempt)
    return dest


# --------------------------------------------------------------------------- #
# Dossier de travail
# --------------------------------------------------------------------------- #

STIGNORE_LINE = f"/{WORK_DIRNAME}"


def ensure_work_area(library: Path) -> Path:
    """Crée <bibliothèque>/.musique/ avec :
    * .nomedia : Android n'indexe pas les fichiers partiels qui s'y trouvent ;
    * une ligne dans .stignore : Syncthing ne synchronise pas ce dossier.
    """
    work = library / WORK_DIRNAME
    (work / "tmp").mkdir(parents=True, exist_ok=True)
    nomedia = work / ".nomedia"
    if not nomedia.exists():
        nomedia.touch()
    ensure_stignore(library)
    return work


def ensure_stignore(library: Path) -> bool:
    """Ajoute STIGNORE_LINE à .stignore si absente. Renvoie True si modifié."""
    st = library / ".stignore"
    lines = st.read_text(encoding="utf-8").splitlines() if st.exists() else []
    if STIGNORE_LINE in (l.strip() for l in lines):
        return False
    lines.append(STIGNORE_LINE)
    st.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


@contextmanager
def temp_dir(library: Path, max_age_h: float = 12) -> Iterator[Path]:
    """Dossier temporaire unique pour un téléchargement, supprimé à la sortie.

    Nettoie aussi les restes de lancements interrompus (plus vieux que max_age_h).
    """
    base = ensure_work_area(library) / "tmp"
    now = time.time()
    for old in base.iterdir():
        try:
            if now - old.stat().st_mtime > max_age_h * 3600:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    d = base / uuid.uuid4().hex[:12]
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Index local (cache reconstructible)
# --------------------------------------------------------------------------- #


@dataclass
class IndexEntry:
    path: str  # relatif à la bibliothèque, séparateur « / »
    mtime: float
    size: int
    source: str | None
    artist_key: str
    title_key: str


class LibraryIndex:
    """Cache local : chemin → (provenance, artiste, titre).

    Stocké dans le dossier d'état de l'appareil (jamais dans la bibliothèque).
    `refresh()` relit seulement les fichiers nouveaux ou modifiés (taille/mtime) ;
    tolérance de 2 s sur mtime car FAT32 n'a qu'une résolution de 2 s.
    """

    VERSION = 1

    def __init__(self, library: Path, state_dir: Path):
        self.library = library
        self.file = state_dir / "index.json"
        self.entries: dict[str, IndexEntry] = {}
        self.queries: dict[str, str] = {}  # clé de requête → chemin relatif
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("version") != self.VERSION or data.get("library") != str(self.library):
            return
        self.entries = {k: IndexEntry(**v) for k, v in data.get("entries", {}).items()}
        self.queries = data.get("queries", {})

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        data = {
            "version": self.VERSION,
            "library": str(self.library),
            "entries": {k: vars(v) for k, v in self.entries.items()},
            "queries": self.queries,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, self.file)

    def refresh(self) -> tuple[int, int]:
        """Rescanne la bibliothèque. Renvoie (fichiers relus, fichiers disparus)."""
        from musique.tagging import read_basic  # import local : mutagen n'est utile qu'ici

        seen: set[str] = set()
        reread = 0
        for path in walk_audio(self.library):
            rel = path.relative_to(self.library).as_posix()
            seen.add(rel)
            st = path.stat()
            old = self.entries.get(rel)
            if old and old.size == st.st_size and abs(old.mtime - st.st_mtime) <= 2:
                continue
            info = read_basic(path) or {}
            self.entries[rel] = IndexEntry(
                path=rel,
                mtime=st.st_mtime,
                size=st.st_size,
                source=info.get("source"),
                artist_key=fold(info.get("artist") or ""),
                title_key=fold(info.get("title") or path.stem),
            )
            reread += 1
        gone = [k for k in self.entries if k not in seen]
        for k in gone:
            del self.entries[k]
        self.queries = {q: p for q, p in self.queries.items() if p in self.entries}
        return reread, len(gone)

    def add(self, path: Path, source: str | None, artist: str, title: str, query_key: str | None) -> None:
        rel = path.relative_to(self.library).as_posix()
        st = path.stat()
        self.entries[rel] = IndexEntry(rel, st.st_mtime, st.st_size, source, fold(artist), fold(title))
        if query_key:
            self.queries[query_key] = rel

    def note_existing(self, path: Path, query_key: str) -> None:
        """Associe une requête à un fichier déjà présent (sans écraser son entrée)."""
        from musique.tagging import read_basic

        rel = path.relative_to(self.library).as_posix()
        if rel not in self.entries:
            info = read_basic(path) or {}
            st = path.stat()
            self.entries[rel] = IndexEntry(rel, st.st_mtime, st.st_size, info.get("source"),
                                           fold(info.get("artist") or ""), fold(info.get("title") or path.stem))
        self.queries[query_key] = rel

    def by_query(self, key: str) -> str | None:
        rel = self.queries.get(key)
        return rel if rel in self.entries else None

    def by_source(self, source_id: str) -> str | None:
        for e in self.entries.values():
            if e.source == source_id:
                return e.path
        return None

    def by_artist_title(self, artist: str, title: str) -> str | None:
        """Fichier dont les tags artiste + titre sont identiques (après normalisation),
        où qu'il soit dans la bibliothèque, même renommé ou déplacé dans un dossier."""
        a, t = fold(artist), fold(title)
        for e in self.entries.values():
            if e.artist_key == a and e.title_key == t:
                return e.path
        return None

    def remember_query(self, key: str, rel: str) -> None:
        self.queries[key] = rel


def walk_audio(root: Path) -> Iterator[Path]:
    """Fichiers audio de la bibliothèque, en ignorant les dossiers cachés (.musique...)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                yield Path(dirpath) / f


# --------------------------------------------------------------------------- #
# Verrou (un seul lancement à la fois par appareil)
# --------------------------------------------------------------------------- #


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def run_lock(state_dir: Path) -> Iterator[None]:
    """Verrou exclusif libéré automatiquement par l'OS si le processus meurt.

    (fcntl sous Linux/Android, msvcrt sous Windows.) Il protège contre deux
    lancements simultanés *sur le même appareil*. Entre deux appareils synchronisés
    par Syncthing, aucun verrou fiable n'est possible (la synchro est différée) :
    la règle est « un seul appareil télécharge à la fois ».
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(state_dir / "musique.lock", "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise AlreadyRunning("musique tourne déjà sur cet appareil") from e
        yield
    finally:
        fh.close()
