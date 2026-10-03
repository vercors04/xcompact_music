"""Source YouTube Music : recherche par ytmusicapi, téléchargement par yt-dlp.

Pourquoi `filter="songs"` : ce filtre ne renvoie que les pistes « audio officielles »
(type MUSIC_VIDEO_TYPE_ATV) fournies par les labels, avec le master du disque. Les
clips (OMV) ont souvent un autre mixage, des bruitages, une intro parlée.

Piège vérifié : dans `get_album`, une piste peut pointer vers le *clip* (OMV) plutôt
que vers la version audio. On ne réutilise donc jamais le videoId de l'album : on
garde celui de la recherche « songs » et on n'utilise l'album que pour l'année, le
numéro de piste et la pochette.

Qualité sans compte : format 251 = Opus ~130-165 kbps, 48 kHz (mesuré). Aucun flux
sans perte n'existe sur YouTube, quel que soit l'outil.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from musique.config import Config
from musique.models import Candidate, Quality, Query, TrackMeta
from musique.textnorm import fold
from musique.ytdlp import ErrorKind, SourceError, download_audio

log = logging.getLogger(__name__)

_GSIZE = re.compile(r"=(?:w\d+-h\d+|s\d+)[^/=]*$")


def sized_cover_url(url: str, size: int, quality: int) -> str:
    """Les pochettes googleusercontent se redimensionnent par un suffixe d'URL.

    >>> sized_cover_url("https://yt3.googleusercontent.com/abc=w544-h544-l90-rj", 800, 75)
    'https://yt3.googleusercontent.com/abc=w800-h800-l75-rj'
    """
    if "googleusercontent.com" not in url:
        return url
    return _GSIZE.sub(f"=w{size}-h{size}-l{quality}-rj", url)


def _year(value) -> int | None:
    m = re.match(r"\d{4}", str(value or ""))
    return int(m.group(0)) if m else None


class YTMusicSource:
    name = "ytmusic"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._yt = None
        self._albums: dict[str, dict] = {}

    @property
    def yt(self):
        if self._yt is None:
            from ytmusicapi import YTMusic

            self._yt = YTMusic()  # anonyme : aucun compte
        return self._yt

    # ------------------------------------------------------------------ #

    def _call(self, what: str, fn, *args, **kwargs):
        """Appel ytmusicapi avec un nouvel essai réseau et des erreurs classées."""
        import requests

        for attempt in range(2):
            try:
                return fn(*args, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 0:
                    log.info("erreur réseau (%s), nouvel essai dans 5 s", what)
                    time.sleep(5)
                    continue
                raise SourceError(ErrorKind.NETWORK, f"{what} : {e}") from e
            except Exception as e:  # format de réponse inattendu → ytmusicapi dépassé
                raise SourceError(
                    ErrorKind.BROKEN,
                    f"{what} : réponse YouTube Music illisible ({type(e).__name__}: {e}). "
                    "Mets à jour ytmusicapi : pip install -U ytmusicapi",
                ) from e
        raise AssertionError("unreachable")

    def search(self, query: Query, limit: int) -> list[Candidate]:
        text = f"{query.artist} {query.title}" if query.artist and query.title else query.raw
        results = self._call("recherche", self.yt.search, text, filter="songs", limit=limit)
        out: list[Candidate] = []
        for r in results:
            vid = r.get("videoId")
            if not vid or r.get("isAvailable") is False:
                continue
            album = r.get("album") or {}
            out.append(
                Candidate(
                    source=self.name,
                    id=vid,
                    title=r.get("title") or "",
                    artists=[a["name"] for a in r.get("artists") or [] if a.get("name")],
                    album=album.get("name"),
                    album_id=album.get("id"),
                    duration=r.get("duration_seconds"),
                    rank=len(out),
                    quality=Quality("opus", 160),  # estimation a priori (format 251)
                    explicit=r.get("isExplicit"),
                )
            )
        return out[:limit]

    def _album(self, album_id: str) -> dict | None:
        if album_id not in self._albums:
            try:
                self._albums[album_id] = self._call("album", self.yt.get_album, album_id)
            except SourceError as e:
                log.info("album %s illisible (%s) : métadonnées réduites", album_id, e)
                self._albums[album_id] = {}
        return self._albums[album_id] or None

    def metadata(self, c: Candidate) -> TrackMeta:
        meta = TrackMeta(
            title=c.title,
            artists=c.artists or ["Artiste inconnu"],
            album=c.album,
            source_id=c.source_id,
            explicit=c.explicit,
        )
        album = self._album(c.album_id) if c.album_id else None
        if not album:
            return meta
        meta.album = album.get("title") or c.album
        meta.album_artists = [a["name"] for a in album.get("artists") or [] if a.get("name")]
        meta.year = _year(album.get("year"))
        meta.track_total = album.get("trackCount")
        rtype = (album.get("type") or "").lower()
        meta.release_type = rtype if rtype in ("album", "single", "ep") else None
        thumbs = sorted(album.get("thumbnails") or [], key=lambda t: t.get("width") or 0)
        if thumbs:
            meta.cover_url = sized_cover_url(thumbs[-1]["url"], self.cfg.cover.size, self.cfg.cover.quality)
        # Numéro de piste : la piste de l'album dont le titre et la durée correspondent.
        want = fold(c.title)
        for t in album.get("tracks") or []:
            same_title = fold(t.get("title") or "") == want
            same_len = c.duration is None or abs((t.get("duration_seconds") or 0) - c.duration) <= 3
            if same_title and same_len:
                meta.track_number = t.get("trackNumber")
                break
        return meta

    def download(self, c: Candidate, dest_dir: Path) -> tuple[Path, Quality]:
        d = self.cfg.download
        ffmpeg_dir = str(Path(self.cfg.ffmpeg).parent) if Path(self.cfg.ffmpeg).parent != Path(".") else ""
        path, fmt = download_audio(
            f"https://music.youtube.com/watch?v={c.id}",
            dest_dir,
            retries=d.retries,
            js_runtime=d.js_runtime,
            cookies_file=d.cookies_file,
            ffmpeg_location=ffmpeg_dir,
        )
        acodec = (fmt.get("acodec") or "").split(".")[0]
        codec = {"mp4a": "aac"}.get(acodec, acodec or path.suffix.lstrip("."))
        return path, Quality(codec, fmt.get("abr"))
