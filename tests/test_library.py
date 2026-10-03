import os

import pytest

from musique.library import (
    STIGNORE_LINE,
    LibraryIndex,
    ensure_stignore,
    place_atomically,
    resolve_existing,
    sanitize_component,
    temp_dir,
    track_relpath,
)
from musique.models import TrackMeta


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AC/DC", "AC-DC"),
        ("Title: Subtitle", "Title - Subtitle"),
        ("What?", "What"),
        ('Say "Hi"', "Say 'Hi'"),
        ("a|b", "a-b"),
        ("Trailing dot.", "Trailing dot"),
        ("...Baby One More Time", "Baby One More Time"),
        ("CON", "CON_"),
        ("nul.txt", "nul.txt_"),
        ("", "_"),
        ("tab\tand\nnewline", "tabandnewline"),
        ("Beyoncé", "Beyoncé"),  # NFD → NFC
    ],
)
def test_sanitize_component(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_respects_utf8_byte_limit():
    s = sanitize_component("é" * 200, max_bytes=150)
    assert len(s.encode("utf-8")) <= 150
    assert s == "é" * 75  # jamais de caractère coupé en deux


def test_track_relpath_standard():
    m = TrackMeta("Around the World", ["Daft Punk"], album="Homework", album_artists=["Daft Punk"], track_number=7)
    assert track_relpath(m, ".opus", "artist_album").as_posix() == "Daft Punk/Homework/07 - Around the World.opus"


def test_track_relpath_uses_album_artist_and_disc():
    m = TrackMeta("Get Lucky", ["Daft Punk", "Pharrell Williams"], album="Random Access Memories",
                  album_artists=["Daft Punk"], track_number=8, disc_number=2)
    assert track_relpath(m, ".opus", "artist_album").as_posix() == "Daft Punk/Random Access Memories/2-08 - Get Lucky.opus"


def test_track_relpath_without_album():
    m = TrackMeta("Song: Part 1", ["X"])
    assert track_relpath(m, ".m4a", "artist_album").as_posix() == "X/[Sans album]/Song - Part 1.m4a"


def test_track_relpath_flat_is_default():
    m = TrackMeta("Around the World", ["Daft Punk"], album="Homework", track_number=7)
    assert track_relpath(m, ".opus").as_posix() == "Daft Punk - Around the World.opus"
    m = TrackMeta("Song: Part 1?", ["AC/DC", "X"])
    assert track_relpath(m, ".opus").as_posix() == "AC-DC, X - Song - Part 1.opus"


def test_track_relpath_unknown_layout():
    with pytest.raises(ValueError):
        track_relpath(TrackMeta("x", ["y"]), ".opus", "nope")


def test_resolve_existing_flat_case_insensitive(tmp_path):
    (tmp_path / "daft punk - around the world.opus").write_bytes(b"x")
    p = resolve_existing(tmp_path, track_relpath(TrackMeta("Around the World", ["Daft Punk"]), ".opus"))
    assert p.exists() and p.name == "daft punk - around the world.opus"


def test_index_finds_moved_file_by_tags(tmp_path):
    """L'utilisateur range lui-même : un fichier déplacé dans un dossier reste reconnu."""
    from musique.library import LibraryIndex

    lib = tmp_path / "lib"
    (lib / "Mes favoris").mkdir(parents=True)
    f = lib / "Mes favoris" / "renommé.opus"
    f.write_bytes(b"x")
    idx = LibraryIndex(lib, tmp_path / "state")
    idx.refresh()
    idx.entries["Mes favoris/renommé.opus"].artist_key = "daft punk"   # comme lu dans les tags
    idx.entries["Mes favoris/renommé.opus"].title_key = "around the world"
    assert idx.by_artist_title("Daft Punk", "Around the World") == "Mes favoris/renommé.opus"
    assert idx.by_artist_title("Daft Punk", "One More Time") is None


def test_resolve_existing_reuses_differently_cased_dirs(tmp_path):
    (tmp_path / "Daft punk" / "homework").mkdir(parents=True)
    p = resolve_existing(tmp_path, track_relpath(
        TrackMeta("X", ["Daft Punk"], album="Homework", track_number=1), ".opus", "artist_album"))
    assert p.parent == tmp_path / "Daft punk" / "homework"


def test_place_atomically_never_overwrites(tmp_path):
    src = tmp_path / "a.opus"
    src.write_bytes(b"1")
    dest = tmp_path / "x" / "y.opus"
    place_atomically(src, dest)
    assert dest.read_bytes() == b"1" and not src.exists()
    src.write_bytes(b"2")
    with pytest.raises(FileExistsError):
        place_atomically(src, dest)
    assert dest.read_bytes() == b"1"


def test_temp_dir_is_inside_library_and_cleaned(tmp_path):
    with temp_dir(tmp_path) as d:
        assert d.parent.parent == tmp_path / ".musique"
        (d / "partial.webm").write_bytes(b"x")
    assert not d.exists()
    assert (tmp_path / ".musique" / ".nomedia").exists()
    assert STIGNORE_LINE in (tmp_path / ".stignore").read_text()


def test_ensure_stignore_is_idempotent_and_keeps_user_lines(tmp_path):
    (tmp_path / ".stignore").write_text("(?d).DS_Store\n")
    assert ensure_stignore(tmp_path) is True
    assert ensure_stignore(tmp_path) is False
    assert (tmp_path / ".stignore").read_text().splitlines() == ["(?d).DS_Store", STIGNORE_LINE]


def test_index_skips_hidden_dirs_and_tracks_changes(tmp_path):
    lib, state = tmp_path / "lib", tmp_path / "state"
    (lib / "A" / "B").mkdir(parents=True)
    (lib / ".musique" / "tmp").mkdir(parents=True)
    (lib / "A" / "B" / "01 - x.opus").write_bytes(b"pas un vrai opus")
    (lib / ".musique" / "tmp" / "partial.opus").write_bytes(b"x")
    idx = LibraryIndex(lib, state)
    assert idx.refresh() == (1, 0)
    assert list(idx.entries) == ["A/B/01 - x.opus"]
    idx.save()
    idx2 = LibraryIndex(lib, state)  # rechargé depuis le disque
    assert idx2.refresh() == (0, 0)
    os.remove(lib / "A" / "B" / "01 - x.opus")
    assert idx2.refresh() == (0, 1)
