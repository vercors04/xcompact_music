"""Écrit puis relit les tags sur de vrais fichiers (générés par ffmpeg)."""

import base64
import shutil
import subprocess

import mutagen
import pytest
from mutagen.flac import Picture

from musique.models import Gain, TrackMeta
from musique.tagging import opus_header_gain_db, read_basic, write_tags

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent")

ENCODERS = {".opus": ["-c:a", "libopus", "-b:a", "64k"], ".m4a": ["-c:a", "aac", "-b:a", "96k"],
            ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"], ".flac": ["-c:a", "flac"]}

# JPEG minimal valide (1×1 px)
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////"
    "////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")

META = TrackMeta(title="Get Lucky", artists=["Daft Punk", "Pharrell Williams"], album="Random Access Memories",
                 album_artists=["Daft Punk"], year=2013, track_number=8, track_total=13, release_type="album",
                 source_id="ytmusic:abc123")
GAIN = Gain(gain_db=-7.3, target_gain_db=-7.3, capped=False, peak_linear=1.513561)


def make(tmp_path, ext):
    p = tmp_path / f"t{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", *ENCODERS[ext], str(p)],
                   check=True)
    return p


@pytest.mark.parametrize("ext", list(ENCODERS))
def test_roundtrip(tmp_path, ext):
    p = make(tmp_path, ext)
    write_tags(p, META, JPEG, GAIN)
    info = read_basic(p)
    assert info["source"] == "ytmusic:abc123"
    assert info["title"] == "Get Lucky"
    assert info["album"] == "Random Access Memories"
    # Le gain relu (avec la conversion des lecteurs pour l'Opus) vaut ce qu'on a écrit.
    assert info["gain_db"] == pytest.approx(-7.3, abs=1 / 256)


@pytest.mark.parametrize("ext", list(ENCODERS))
def test_musicbrainz_ids_and_retag_roundtrip(tmp_path, ext):
    from dataclasses import replace

    from musique.tagging import read_track_meta

    p = make(tmp_path, ext)
    meta = replace(META, mbids={"track": "rec-1", "release": "rel-1", "release_group": "rg-1",
                                "artists": ["a-1", "a-2"]})
    write_tags(p, meta, JPEG, GAIN)
    back, duration = read_track_meta(p)
    assert back.title == "Get Lucky" and back.artists == ["Daft Punk", "Pharrell Williams"]
    assert back.album == "Random Access Memories" and back.year == 2013
    assert back.source_id == "ytmusic:abc123"
    assert duration == pytest.approx(1.0, abs=0.1)
    if ext in (".opus", ".flac"):
        tags = mutagen.File(p).tags
        assert tags["MUSICBRAINZ_TRACKID"] == ["rec-1"]
        assert tags["MUSICBRAINZ_ARTISTID"] == ["a-1", "a-2"]


def test_opus_uses_r128_only(tmp_path):
    p = make(tmp_path, ".opus")
    write_tags(p, META, JPEG, GAIN)
    tags = mutagen.File(p).tags
    assert tags["R128_TRACK_GAIN"] == ["-3149"]
    assert not any(k.upper().startswith("REPLAYGAIN") for k in tags.keys())
    assert tags["ARTISTS"] == ["Daft Punk", "Pharrell Williams"]
    assert tags["ARTIST"] == ["Daft Punk, Pharrell Williams"]
    pic = Picture(base64.b64decode(tags["METADATA_BLOCK_PICTURE"][0]))
    assert pic.type == 3 and pic.mime == "image/jpeg" and pic.data == JPEG
    assert opus_header_gain_db(p) == 0.0  # ffmpeg (comme YouTube) laisse l'output gain à 0


def test_flac_uses_replaygain(tmp_path):
    p = make(tmp_path, ".flac")
    write_tags(p, META, JPEG, GAIN)
    f = mutagen.File(p)
    assert f.tags["REPLAYGAIN_TRACK_GAIN"] == ["-7.30 dB"]
    assert f.tags["REPLAYGAIN_TRACK_PEAK"] == ["1.513561"]
    assert "R128_TRACK_GAIN" not in f.tags
    assert f.pictures[0].data == JPEG


def test_gain_only_update_keeps_metadata_and_cover(tmp_path):
    p = make(tmp_path, ".opus")
    write_tags(p, META, JPEG, GAIN)
    write_tags(p, None, None, Gain(-2.0, -2.0, False, 0.5))  # `musique gain --all`
    tags = mutagen.File(p).tags
    assert tags["R128_TRACK_GAIN"] == [str(round((-2.0 - 5) * 256))]
    assert tags["TITLE"] == ["Get Lucky"] and "METADATA_BLOCK_PICTURE" in tags


def test_meta_rewrite_drops_source_junk_but_keeps_cover(tmp_path):
    p = make(tmp_path, ".opus")
    f = mutagen.File(p)
    f.tags["DESCRIPTION"] = ["Provided to YouTube by ..."]
    f.tags["ARTIST"] = ["Daft Punk - Topic"]
    f.save()
    write_tags(p, None, JPEG, None)
    write_tags(p, META, None, None)
    tags = mutagen.File(p).tags
    assert "DESCRIPTION" not in tags
    assert tags["ARTIST"] == ["Daft Punk, Pharrell Williams"]
    assert "METADATA_BLOCK_PICTURE" in tags


def test_mp4_and_mp3_cover(tmp_path):
    p = make(tmp_path, ".m4a")
    write_tags(p, META, JPEG, GAIN)
    assert bytes(mutagen.File(p).tags["covr"][0]) == JPEG
    p = make(tmp_path, ".mp3")
    write_tags(p, META, JPEG, GAIN)
    f = mutagen.File(p)
    assert f.tags.getall("APIC")[0].data == JPEG
    assert f.tags["TPE1"].text == ["Daft Punk", "Pharrell Williams"]
