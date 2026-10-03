"""Écriture/lecture des tags avec mutagen, pour chaque format.

Ce qu'on écrit (noms Vorbis ; équivalents MP4/ID3 plus bas) :
  TITLE, ARTIST (affichage « A, B »), ARTISTS (une valeur par artiste),
  ALBUM, ALBUMARTIST, ALBUMARTISTS, DATE, ORIGINALDATE, TRACKNUMBER, TRACKTOTAL,
  DISCNUMBER, RELEASETYPE, pochette, gain, et MUSIQUE_SOURCE (provenance).
Auxio lit ARTISTS en priorité puis ARTIST (vérifié dans son code source).

Le gain :
* Opus : uniquement R128_TRACK_GAIN (RFC 7845). Les REPLAYGAIN_* sont proscrits
  dans l'Opus par la spec, on les supprime s'ils existent.
* Vorbis/FLAC : REPLAYGAIN_TRACK_GAIN (« -7.30 dB ») et REPLAYGAIN_TRACK_PEAK
  (amplitude linéaire).
* MP4 : atomes « ----:com.apple.iTunes:REPLAYGAIN_TRACK_* ».
* MP3 : cadres ID3v2.4 TXXX:REPLAYGAIN_TRACK_*.
On efface toujours les valeurs « album » : nos fichiers ne sont pas des albums
complets, un gain album serait faux.
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from musique.loudness import r128_q78
from musique.models import Gain, TrackMeta

SOURCE_TAG = "MUSIQUE_SOURCE"
_ITUNES = "----:com.apple.iTunes:"


class TagError(RuntimeError):
    pass


def _image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _flac_picture(data: bytes) -> Picture:
    pic = Picture()
    pic.type = 3  # « Cover (front) »
    pic.mime = _image_mime(data)
    pic.desc = "Cover"
    pic.data = data
    return pic


def _vorbis_fields(meta: TrackMeta) -> dict[str, list[str]]:
    f: dict[str, list[str]] = {
        "TITLE": [meta.title],
        "ARTIST": [meta.artist_display],
        "ARTISTS": list(meta.artists),
    }
    if meta.album:
        f["ALBUM"] = [meta.album]
        f["ALBUMARTIST"] = [meta.album_artist_display]
        f["ALBUMARTISTS"] = list(meta.album_artists or meta.artists[:1])
    if meta.year:
        f["DATE"] = [str(meta.year)]
        f["ORIGINALDATE"] = [str(meta.year)]
    if meta.track_number:
        f["TRACKNUMBER"] = [str(meta.track_number)]
    if meta.track_total:
        f["TRACKTOTAL"] = [str(meta.track_total)]
    if meta.disc_number:
        f["DISCNUMBER"] = [str(meta.disc_number)]
    if meta.release_type:
        f["RELEASETYPE"] = [meta.release_type]
    if meta.source_id:
        f[SOURCE_TAG] = [meta.source_id]
    for key, tag in _MB_VORBIS.items():
        value = meta.mbids.get(key)
        if value:
            f[tag] = list(value) if isinstance(value, list) else [value]
    return f


# Noms utilisés par MusicBrainz Picard (lus par Auxio, Strawberry, beets...).
_MB_VORBIS = {
    "track": "MUSICBRAINZ_TRACKID",
    "release": "MUSICBRAINZ_ALBUMID",
    "release_group": "MUSICBRAINZ_RELEASEGROUPID",
    "artists": "MUSICBRAINZ_ARTISTID",
}
_MB_FREEFORM = {
    "track": "MusicBrainz Track Id",
    "release": "MusicBrainz Album Id",
    "release_group": "MusicBrainz Release Group Id",
    "artists": "MusicBrainz Artist Id",
}


_GAIN_KEYS = (
    "REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_GAIN",
    "REPLAYGAIN_ALBUM_PEAK", "REPLAYGAIN_REFERENCE_LOUDNESS", "R128_TRACK_GAIN", "R128_ALBUM_GAIN",
)


def _rg_strings(gain: Gain) -> tuple[str, str]:
    return f"{gain.gain_db:.2f} dB", f"{gain.peak_linear:.6f}"


# --------------------------------------------------------------------------- #
# Écriture
# --------------------------------------------------------------------------- #


def write_tags(path: Path, meta: TrackMeta | None, cover: bytes | None, gain: Gain | None) -> None:
    """Écrit métadonnées, pochette et gain. `None` = ne pas toucher à cette partie."""
    f = mutagen.File(path)
    if f is None:
        raise TagError(f"format non reconnu : {path.name}")
    if isinstance(f, (OggOpus, OggVorbis, FLAC)):
        _write_vorbis(f, meta, cover, gain, opus=isinstance(f, OggOpus))
    elif isinstance(f, MP4):
        _write_mp4(f, meta, cover, gain)
    elif isinstance(f, MP3):
        _write_mp3(path, meta, cover, gain)
        return
    else:
        raise TagError(f"format non géré : {type(f).__name__}")
    f.save()


def _write_vorbis(f, meta, cover, gain, opus: bool) -> None:
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    if meta is not None:
        # On repart de zéro (sauf le gain, traité à part) : les tags éventuels de la
        # source (« Daft Punk - Topic », description YouTube...) ne nous intéressent pas.
        for k in list(tags.keys()):
            if k.upper() not in _GAIN_KEYS and k.upper() != "METADATA_BLOCK_PICTURE":
                del tags[k]
        for k, v in _vorbis_fields(meta).items():
            tags[k] = v
    if cover is not None:
        if isinstance(f, FLAC):
            f.clear_pictures()
            f.add_picture(_flac_picture(cover))
        else:
            # Ogg : l'image est un bloc « picture » FLAC encodé en base64 dans un tag.
            tags["METADATA_BLOCK_PICTURE"] = [base64.b64encode(_flac_picture(cover).write()).decode("ascii")]
    if gain is not None:
        for k in _GAIN_KEYS:
            if k in tags:
                del tags[k]
        if opus:
            tags["R128_TRACK_GAIN"] = [str(r128_q78(gain.gain_db))]
        else:
            g, p = _rg_strings(gain)
            tags["REPLAYGAIN_TRACK_GAIN"] = [g]
            tags["REPLAYGAIN_TRACK_PEAK"] = [p]


def _write_mp4(f: MP4, meta, cover, gain) -> None:
    if f.tags is None:
        f.add_tags()
    t = f.tags
    if meta is not None:
        for k in list(t.keys()):
            if not k.startswith(_ITUNES + "REPLAYGAIN") and k != "covr":
                del t[k]
        t["©nam"] = [meta.title]
        t["©ART"] = [meta.artist_display]
        t[_ITUNES + "ARTISTS"] = [MP4FreeForm(a.encode()) for a in meta.artists]
        if meta.album:
            t["©alb"] = [meta.album]
            t["aART"] = [meta.album_artist_display]
        if meta.year:
            t["©day"] = [str(meta.year)]
        if meta.track_number:
            t["trkn"] = [(meta.track_number, meta.track_total or 0)]
        if meta.disc_number:
            t["disk"] = [(meta.disc_number, 0)]
        if meta.release_type:
            t[_ITUNES + "MusicBrainz Album Type"] = [MP4FreeForm(meta.release_type.encode())]
        if meta.source_id:
            t[_ITUNES + SOURCE_TAG] = [MP4FreeForm(meta.source_id.encode())]
        for key, name in _MB_FREEFORM.items():
            value = meta.mbids.get(key)
            if value:
                values = value if isinstance(value, list) else [value]
                t[_ITUNES + name] = [MP4FreeForm(v.encode()) for v in values]
    if cover is not None:
        fmt = MP4Cover.FORMAT_PNG if _image_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(cover, imageformat=fmt)]
    if gain is not None:
        for k in list(t.keys()):
            if k.upper().startswith((_ITUNES + "REPLAYGAIN").upper()):
                del t[k]
        g, p = _rg_strings(gain)
        t[_ITUNES + "REPLAYGAIN_TRACK_GAIN"] = [MP4FreeForm(g.encode())]
        t[_ITUNES + "REPLAYGAIN_TRACK_PEAK"] = [MP4FreeForm(p.encode())]


def _write_mp3(path: Path, meta, cover, gain) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if meta is not None:
        for key in list(tags.keys()):
            if not key.upper().startswith(("TXXX:REPLAYGAIN", "APIC")):
                del tags[key]
        tags.add(TIT2(encoding=3, text=meta.title))
        tags.add(TPE1(encoding=3, text=list(meta.artists)))  # v2.4 : multi-valeurs
        tags.add(TXXX(encoding=3, desc="ARTISTS", text=list(meta.artists)))
        if meta.album:
            tags.add(TALB(encoding=3, text=meta.album))
            tags.add(TPE2(encoding=3, text=meta.album_artist_display))
        if meta.year:
            tags.add(TDRC(encoding=3, text=str(meta.year)))
            tags.add(TDOR(encoding=3, text=str(meta.year)))
        if meta.track_number:
            trck = f"{meta.track_number}/{meta.track_total}" if meta.track_total else str(meta.track_number)
            tags.add(TRCK(encoding=3, text=trck))
        if meta.disc_number:
            tags.add(TPOS(encoding=3, text=str(meta.disc_number)))
        if meta.release_type:
            tags.add(TXXX(encoding=3, desc="MusicBrainz Album Type", text=meta.release_type))
        if meta.source_id:
            tags.add(TXXX(encoding=3, desc=SOURCE_TAG, text=meta.source_id))
        for key, name in _MB_FREEFORM.items():
            value = meta.mbids.get(key)
            if value and key != "track":
                tags.add(TXXX(encoding=3, desc=name, text=value if isinstance(value, list) else [value]))
        if meta.mbids.get("track"):
            tags.add(UFID(owner="http://musicbrainz.org", data=meta.mbids["track"].encode()))
    if cover is not None:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=cover))
    if gain is not None:
        for key in list(tags.keys()):
            if key.upper().startswith("TXXX:REPLAYGAIN"):
                del tags[key]
        g, p = _rg_strings(gain)
        tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=g))
        tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=p))
    tags.save(path, v2_version=4)


# --------------------------------------------------------------------------- #
# Lecture
# --------------------------------------------------------------------------- #


def read_basic(path: Path) -> dict | None:
    """Lit provenance, titre, artiste, album et gain. None si illisible.

    Sert à l'index de la bibliothèque (idempotence) et à `musique gain --check`.
    """
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None or f.tags is None:
        return {"source": None, "title": None, "artist": None, "album": None, "gain_db": None}

    def first(*keys: str) -> str | None:
        for k in keys:
            try:
                v = f.tags[k]
            except (KeyError, ValueError):
                continue
            if isinstance(v, list):
                v = v[0] if v else None
            if v is None:
                continue
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            if hasattr(v, "text"):  # cadre ID3
                v = v.text[0] if v.text else None
            if v is not None:
                return str(v)
        return None

    if isinstance(f, MP4):
        out = {
            "source": first(_ITUNES + SOURCE_TAG),
            "title": first("©nam"),
            "artist": first("©ART"),
            "album": first("©alb"),
        }
        g = first(_ITUNES + "REPLAYGAIN_TRACK_GAIN")
    elif isinstance(f, MP3):
        out = {
            "source": first(f"TXXX:{SOURCE_TAG}"),
            "title": first("TIT2"),
            "artist": first("TPE1"),
            "album": first("TALB"),
        }
        g = first("TXXX:REPLAYGAIN_TRACK_GAIN")
    else:
        out = {
            "source": first(SOURCE_TAG),
            "title": first("TITLE"),
            "artist": first("ARTIST"),
            "album": first("ALBUM"),
        }
        g = first("REPLAYGAIN_TRACK_GAIN")
        r128 = first("R128_TRACK_GAIN")
        if r128 is not None:
            try:
                out["gain_db"] = int(r128) / 256 + 5.0  # même conversion qu'Auxio/GStreamer
                return out
            except ValueError:
                pass
    out["gain_db"] = _parse_db(g)
    return out


def read_track_meta(path: Path) -> tuple[TrackMeta, float | None] | None:
    """Relit un fichier sous forme de TrackMeta (+ durée en s), pour `musique retag`."""
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None or f.tags is None:
        return None
    t = f.tags

    def values(*keys: str) -> list[str]:
        for k in keys:
            try:
                v = t[k]
            except (KeyError, ValueError):
                continue
            if hasattr(v, "text"):
                v = v.text
            v = v if isinstance(v, list) else [v]
            out = [x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x) for x in v if x is not None]
            if out:
                return out
        return []

    if isinstance(f, MP4):
        title, artists = values("©nam"), values(_ITUNES + "ARTISTS", "©ART")
        album, date, src = values("©alb"), values("©day"), values(_ITUNES + SOURCE_TAG)
        trkn = t.get("trkn") or [(None, None)]
        number, total = trkn[0][0], trkn[0][1] or None
        mb_track, mb_release = values(_ITUNES + "MusicBrainz Track Id"), values(_ITUNES + "MusicBrainz Album Id")
    elif isinstance(f, MP3):
        title, artists = values("TIT2"), values("TPE1")
        album, date, src = values("TALB"), values("TDRC"), values(f"TXXX:{SOURCE_TAG}")
        number, _, total = (values("TRCK") or [""])[0].partition("/")
        ufid = t.get("UFID:http://musicbrainz.org")
        mb_track = [ufid.data.decode()] if ufid else []
        mb_release = values("TXXX:MusicBrainz Album Id")
    else:
        title, artists = values("TITLE"), values("ARTISTS", "ARTIST")
        album, date, src = values("ALBUM"), values("DATE"), values(SOURCE_TAG)
        number, total = (values("TRACKNUMBER") or [None])[0], (values("TRACKTOTAL") or [None])[0]
        mb_track, mb_release = values("MUSICBRAINZ_TRACKID"), values("MUSICBRAINZ_ALBUMID")
    if not title or not artists:
        return None
    year = int(date[0][:4]) if date and date[0][:4].isdigit() else None
    mbids = {k: v[0] for k, v in (("track", mb_track), ("release", mb_release)) if v}
    meta = TrackMeta(title=title[0], artists=artists, album=album[0] if album else None, year=year,
                     track_number=_to_int(number), track_total=_to_int(total),
                     source_id=src[0] if src else None, mbids=mbids)
    return meta, getattr(f.info, "length", None)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def has_cover(path: Path) -> bool:
    """Le fichier contient-il déjà une pochette ?"""
    try:
        f = mutagen.File(path)
    except Exception:
        return False
    if f is None:
        return False
    if isinstance(f, FLAC):
        return bool(f.pictures)
    tags = f.tags or {}
    if isinstance(f, MP4):
        return "covr" in tags
    if isinstance(f, MP3):
        return bool(tags.getall("APIC"))
    return "METADATA_BLOCK_PICTURE" in tags


def same_metadata(a: TrackMeta, b: TrackMeta) -> bool:
    """Les champs que `retag` écrit sont-ils déjà identiques ? (évite de réécrire, et donc
    de faire renvoyer par Syncthing, des fichiers qui n'ont pas changé)"""
    def key(m: TrackMeta) -> tuple:
        return (m.title, list(m.artists), m.album, m.year, m.track_number, m.track_total,
                m.mbids.get("track"), m.mbids.get("release"))
    return key(a) == key(b)


def _parse_db(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.lower().replace("db", "").strip())
    except ValueError:
        return None


def opus_header_gain_db(path: Path) -> float:
    """« Output gain » de l'en-tête OpusHead (RFC 7845 §5.1), en dB.

    Ce gain est appliqué par *tous* les décodeurs (Android, GStreamer, ffmpeg) avant
    les tags. YouTube le laisse à 0 ; on le lit pour le vérifier.
    """
    with open(path, "rb") as fh:
        head = fh.read(4096)
    i = head.find(b"OpusHead")
    if i < 0 or len(head) < i + 18:
        raise TagError("en-tête OpusHead introuvable")
    (q78,) = struct.unpack_from("<h", head, i + 16)
    return q78 / 256
