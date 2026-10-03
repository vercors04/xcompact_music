"""Choix manuel de fin de lot, recherche fusionnée, invités et playlists."""

import pytest

from musique.matching import rank
from musique.models import Candidate, Outcome, Quality, Query, TrackMeta

from test_robustness import FakeSource, make_ctx


def cand(vid, title, artists, album=None, duration=None):
    return Candidate("ytmusic", vid, title, artists, album=album, duration=duration, quality=Quality("opus", 160))


# --------------------------------------------------------------------------- #
# Lecture de la sélection
# --------------------------------------------------------------------------- #


def test_parse_selection_numbers_letters_ranges():
    from musique.cli import parse_selection

    offered = {2: 3, 5: 1, 15: 2}
    assert parse_selection("2 15", offered) == [(2, 0), (15, 0)]
    assert parse_selection("15b,2c", offered) == [(15, 1), (2, 2)]
    assert parse_selection("1-20", offered) == [(2, 0), (5, 0), (15, 0)]
    assert parse_selection("2 2 2a", offered) == [(2, 0)]  # doublons ignorés
    assert parse_selection("  ", offered) == []
    assert parse_selection("tout", offered) == [(2, 0), (5, 0), (15, 0)]


@pytest.mark.parametrize("text", ["3", "5b", "2d", "x", "6-4", "2-5b", "15 abc"])
def test_parse_selection_rejects_invalid(text):
    from musique.cli import parse_selection

    with pytest.raises(ValueError):
        parse_selection(text, {2: 3, 5: 1, 15: 2})


# --------------------------------------------------------------------------- #
# Pipeline : téléchargement d'un candidat choisi à la main
# --------------------------------------------------------------------------- #


def fake_acquire(calls):
    """Remplace _acquire : crée un fichier et l'indexe, comme le vrai."""

    def acquire(s, q, ctx):
        from musique.query import query_key

        calls.append(s.candidate.id)
        f = ctx.cfg.library / f"{s.candidate.id}.opus"
        f.write_bytes(b"x")
        ctx.index.add(f, s.candidate.source_id, ", ".join(s.candidate.artists), s.candidate.title, query_key(q))
        return f, "ok"

    return acquire


HARDCORE = cand("v2", "Colors (Hardcore Remix)", ["Black Pumas"])
LIVE = cand("v3", "Colors (Live)", ["Black Pumas"])


def test_process_chosen_downloads_a_doubtful_candidate(tmp_path, monkeypatch):
    from musique import pipeline

    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    calls = []
    monkeypatch.setattr(pipeline, "_acquire", fake_acquire(calls))
    q = Query("Black Pumas - Colors", "Black Pumas", "Colors")
    ctx = make_ctx(tmp_path, FakeSource(candidates=[HARDCORE, LIVE]))

    r = pipeline.process(q, ctx)
    assert r.outcome is Outcome.DOUBTFUL and len(ctx.pending.items) == 1
    assert {s.candidate.id for s in r.choices} == {"v2", "v3"}
    assert calls == []

    chosen = next(s for s in r.choices if s.candidate.id == "v2")
    r2 = pipeline.process_chosen(q, chosen, ctx)
    assert r2.outcome is Outcome.DOWNLOADED and calls == ["v2"]
    assert ctx.pending.items == {}  # sorti des douteux
    # Relancer la même requête ne redemande rien : la requête est mémorisée.
    assert pipeline.process(q, ctx).outcome is Outcome.PRESENT


def test_manual_no_offers_nothing(tmp_path, monkeypatch):
    """« aucun » répondu avec --confirm : rien à reproposer en fin de lot."""
    from musique import pipeline

    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    ctx = make_ctx(tmp_path, FakeSource(candidates=[HARDCORE]))
    ctx.confirm = lambda q, ranked: None
    r = pipeline.process(Query("Black Pumas - Colors", "Black Pumas", "Colors"), ctx)
    assert r.outcome is Outcome.NOT_FOUND and r.choices == []


def test_cli_end_of_batch_choice(tmp_path, monkeypatch, capsys):
    """Lot complet : un morceau sûr téléchargé, un douteux choisi à la fin (« 2 »)."""
    import musique.sources
    from musique import cli, pipeline

    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    conf = tmp_path / "c.toml"
    conf.write_text(f"library = '{lib}'\nstate_dir = '{tmp_path / 'state'}'\nwake_lock = false\n"
                    "[download]\nsleep_between_s = 0.0\n", encoding="utf-8")

    class Src:
        name = "ytmusic"

        def search(self, q, limit):
            if q.title == "Lean on Me":
                return [cand("ok1", "Lean on Me", ["Bill Withers"])]
            return [HARDCORE]

    calls = []
    monkeypatch.setattr(musique.sources, "build_sources", lambda cfg: [Src()])
    monkeypatch.setattr(pipeline, "_acquire", fake_acquire(calls))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    answers = iter(["9", "2"])  # 9 : pas proposé → redemande ; 2 : le douteux
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    code = cli.main(["get", "Bill Withers - Lean on Me", "Black Pumas - Colors", "--config", str(conf)])
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["ok1", "v2"]
    assert "a) Black Pumas - Colors (Hardcore Remix)" in out
    assert "pas dans la liste" in out
    assert "Résumé : 2 téléchargé" in out


def test_no_prompt_without_terminal(tmp_path, monkeypatch, capsys):
    from musique.cli import offer_choices
    from musique.models import Result, Scored

    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("ne doit rien demander"))
    s = Scored(HARDCORE, 0.62, {})
    r = Result(Query("Black Pumas - Colors", "Black Pumas", "Colors"), Outcome.DOUBTFUL, best=s, choices=[s])
    assert offer_choices([r]) == []
    assert "Hardcore Remix" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Recherche YouTube Music : deux formulations fusionnées
# --------------------------------------------------------------------------- #


class FakeYT:
    """Réponses réelles abrégées (2026-10-03) : sans tiret, l'original n'apparaît pas."""

    def __init__(self):
        self.texts = []

    def search(self, text, filter=None, limit=10):
        self.texts.append(text)

        def song(vid, title, album, dur):
            return {"videoId": vid, "title": title, "artists": [{"name": "Black Pumas"}],
                    "album": {"name": album, "id": None}, "duration_seconds": dur}

        live = song("UoJ", "Colors (Live In Studio)", "Black Pumas (Deluxe Edition)", 384)
        brass = song("ExQ", "Colors (feat. Hypnotic Brass Ensemble)", "Colors (Hypnotic Brass) b/w Sugar Man", 270)
        if " - " in text:
            return [song("wyD", "Colors", "Black Pumas", 247), live]
        return [live, song("_vZ", "Colors (Acoustic)", "Colors (Acoustic)", 324), brass]


def test_search_merges_both_phrasings():
    from musique.config import Config
    from musique.sources.ytmusic import YTMusicSource

    src = YTMusicSource(Config(library=None))
    src._yt = FakeYT()
    q = Query("Black Pumas - Colors", "Black Pumas", "Colors")
    cands = src.search(q, 10)
    assert src._yt.texts == ["Black Pumas Colors", "Black Pumas - Colors"]
    assert sorted(c.id for c in cands) == ["ExQ", "UoJ", "_vZ", "wyD"]  # sans doublon
    assert {c.id: c.rank for c in cands}["wyD"] == 0
    best = rank(q, cands)[0]
    assert best.candidate.id == "wyD" and best.score >= 0.85


def test_one_failed_phrasing_keeps_the_other(monkeypatch):
    import requests

    from musique.config import Config
    from musique.sources import ytmusic
    from musique.sources.ytmusic import YTMusicSource
    from musique.ytdlp import SourceError

    monkeypatch.setattr(ytmusic.time, "sleep", lambda s: None)
    yt = FakeYT()
    real = yt.search

    def flaky(text, **kw):
        if " - " in text:
            raise requests.ConnectionError("coupure")
        return real(text, **kw)

    yt.search = flaky
    src = YTMusicSource(Config(library=None))
    src._yt = yt
    q = Query("Black Pumas - Colors", "Black Pumas", "Colors")
    assert sorted(c.id for c in src.search(q, 10)) == ["ExQ", "UoJ", "_vZ"]

    yt.search = lambda text, **kw: (_ for _ in ()).throw(requests.ConnectionError("coupure"))
    with pytest.raises(SourceError):
        src.search(q, 10)


def test_free_text_searches_once():
    from musique.config import Config
    from musique.sources.ytmusic import YTMusicSource

    src = YTMusicSource(Config(library=None))
    src._yt = FakeYT()
    src.search(Query("black pumas colors"), 10)
    assert src._yt.texts == ["black pumas colors"]


def test_without_solo_original_the_guest_version_wins_but_not_with_it():
    """Ce qui s'est passé avant la correction : sans l'original dans les résultats, la
    version « feat. Hypnotic Brass » passait à 0,97. Avec l'original, elle perd."""
    q = Query("Black Pumas - Colors", "Black Pumas", "Colors")
    live = cand("UoJ", "Colors (Live In Studio)", ["Black Pumas"])
    brass = cand("ExQ", "Colors (feat. Hypnotic Brass Ensemble)", ["Black Pumas"])
    orig = cand("wyD", "Colors", ["Black Pumas"])
    assert rank(q, [live, brass])[0].candidate.id == "ExQ"
    ranked = rank(q, [live, brass, orig])
    assert ranked[0].candidate.id == "wyD"
    assert next(s for s in ranked if s.candidate.id == "ExQ").score < 0.85


# --------------------------------------------------------------------------- #
# Artistes invités
# --------------------------------------------------------------------------- #


def test_requested_guest_in_title_counts_as_artist():
    from musique.matching import score_candidate

    q = Query("Nouvelle Vague, Camille - In a Manner of Speaking", "Nouvelle Vague, Camille",
              "In a Manner of Speaking")
    s = score_candidate(q, cand("x", "In a Manner of Speaking (feat. Camille)", ["Nouvelle Vague"]))
    assert s.details["artist"] == 1.0 and s.score >= 0.99  # 0,89 avant


def test_guest_dropped_from_title_moves_to_artists():
    from musique.musicbrainz import ReleaseChoice, apply_choice

    rec = {"id": "r", "title": "Résonances", "artist-credit": [{"name": "Constance Amiot", "artist": {"id": "a"}}]}
    rel = {"id": "rel", "title": "Blue Green Tomorrows", "date": "2012", "release-group": {"id": "rg"}}
    meta = TrackMeta("Résonances (feat. JP Nataf)", ["Constance Amiot"])
    m = apply_choice(meta, ReleaseChoice(rec, rel, {"position": 1}, {"number": "3"}))
    assert m.title == "Résonances"
    assert m.artists == ["Constance Amiot", "JP Nataf"]
    assert m.album_artists == ["Constance Amiot"]


def test_guest_already_credited_is_not_duplicated():
    from musique.musicbrainz import ReleaseChoice, apply_choice

    rec = {"id": "r", "title": "Get Lucky", "artist-credit": []}
    rel = {"id": "rel", "title": "Random Access Memories", "date": "2013", "release-group": {"id": "rg"}}
    meta = TrackMeta("Get Lucky (feat. Pharrell Williams and Nile Rodgers)", ["Daft Punk", "Pharrell Williams"])
    m = apply_choice(meta, ReleaseChoice(rec, rel, {}, {"number": "8"}))
    assert m.artists == ["Daft Punk", "Pharrell Williams", "Nile Rodgers"]


# --------------------------------------------------------------------------- #
# Playlists : la durée d'un clip n'est pas celle du morceau
# --------------------------------------------------------------------------- #


def test_playlist_video_duration_is_ignored(monkeypatch):
    import ytmusicapi

    from musique.cli import playlist_queries

    tracks = [
        {"title": "Colors", "artists": [{"name": "Black Pumas"}], "videoType": "MUSIC_VIDEO_TYPE_ATV",
         "duration_seconds": 247, "album": {"name": "Black Pumas"}},
        {"title": "Colors (Official Music Video)", "artists": [{"name": "Black Pumas"}],
         "videoType": "MUSIC_VIDEO_TYPE_OMV", "duration_seconds": 265},
        {"title": "Daft Punk - Around The World (Official Video)", "artists": [{"name": "DaftPunkVEVO"}],
         "videoType": "MUSIC_VIDEO_TYPE_OMV", "duration_seconds": 240},
    ]

    class YT:
        def get_playlist(self, pid, limit=None):
            assert pid == "PLx"
            return {"tracks": tracks}

    monkeypatch.setattr(ytmusicapi, "YTMusic", YT)
    qs = playlist_queries("https://music.youtube.com/playlist?list=PLx")
    assert [(q.artist, q.title, q.duration) for q in qs] == [
        ("Black Pumas", "Colors", 247),
        ("Black Pumas", "Colors", None),
        ("Daft Punk", "Around The World", None),
    ]
