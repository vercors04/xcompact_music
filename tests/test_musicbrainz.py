"""Rejoue de vraies réponses MusicBrainz (capturées le 2026-10-03, tests/data/capture_mb.py)."""

import json
from pathlib import Path

import pytest

from musique.matching import MatchConfig
from musique.models import TrackMeta
from musique.musicbrainz import (
    MBError,
    choose_release,
    enrich,
    prefer_case,
    release_sort_key,
    track_number,
)

CASES = json.loads((Path(__file__).parent / "data" / "mb_cases.json").read_text(encoding="utf-8"))


class ReplayClient:
    """Client factice : renvoie, dans l'ordre, les réponses enregistrées."""

    def __init__(self, calls):
        self.calls = list(calls)
        self.queries = []

    def get(self, path, **params):
        expected = self.calls.pop(0)
        assert expected["path"] == path and expected["query"] == params.get("query")
        self.queries.append(params.get("query"))
        return expected["response"]

    def search_recordings(self, query, limit=100):
        return self.get("recording", query=query, limit=limit).get("recordings", [])

    def search_artists(self, name, limit=5):
        from musique.musicbrainz import _lucene_escape

        return self.get("artist", query=_lucene_escape(name), limit=limit).get("artists", [])

    def cover(self, rgid, size):
        return None


def run(name):
    title, artists, album, year, dur = CASES[name]["input"]
    client = ReplayClient(CASES[name]["calls"])
    e = enrich(TrackMeta(title, artists, album=album, year=year), dur, client, MatchConfig())
    assert not client.calls, "toutes les requêtes enregistrées doivent être consommées"
    return e


def test_creep_goes_to_pablo_honey_track_2():
    # YouTube Music : single « Creep » (1992). Attendu : l'album Pablo Honey, piste 2,
    # et surtout pas la piste 13 « Creep (radio edit) » d'une édition américaine.
    e = run("creep")
    m = e.meta
    assert e.used_musicbrainz
    assert (m.album, m.year, m.track_number, m.track_total, m.release_type) == ("Pablo Honey", 1993, 2, 12, "album")
    assert m.title == "Creep" and m.artists == ["Radiohead"]
    assert set(m.mbids) == {"track", "release", "release_group", "artists"}


def test_homework_year_fixed():
    m = run("around").meta  # YouTube Music disait 1996
    assert (m.album, m.year, m.track_number, m.track_total) == ("Homework", 1997, 7, 16)


def test_alias_fallback_and_original_album():
    # « Haruomi Hosono » n'est qu'un alias de « 細野晴臣 » : 1re recherche vide, repli par artiste.
    e = run("sportsmen")
    m = e.meta
    assert len(e.queries) == 2 and "arid:" in e.queries[1]
    assert m.album == "PHILHARMONY" and m.year == 1982  # album original, pas la compilation YMO
    assert m.title == "Sports Men"  # titre propre (sans « 2018 ... Remastering »), pas en CAPITALES
    assert m.artists == ["Haruomi Hosono"]  # noms latins de YouTube Music conservés
    assert m.album_artists == ["Haruomi Hosono"]
    assert m.track_number == 8


def test_vinyl_numbering_uses_offset():
    m = run("queen").meta  # 1975 : seul le vinyle existe, numéroté « B4 »
    assert (m.album, m.year, m.track_number) == ("A Night at the Opera", 1975, 11)


def test_variant_kept_in_title_when_mb_lacks_it():
    m = run("stromae").meta
    assert m.title == "Alors on danse (Radio Edit)"
    assert (m.album, m.year) == ("Cheese", 2010)


def test_requested_variant_finds_its_own_release():
    m = run("creep_acoustic").meta
    assert m.album == "Creep EP" and m.release_type == "ep"
    assert "acoustic" in m.title.lower()


def test_failure_keeps_youtube_metadata():
    class Down:
        def search_recordings(self, *a, **k):
            raise MBError("HTTP 503")

    meta = TrackMeta("Creep", ["Radiohead"], album="Creep", year=1992)
    e = enrich(meta, 239, Down(), MatchConfig())
    assert e.meta is meta and not e.used_musicbrainz and "indisponible" in e.note


def test_client_retries_then_circuit_breaker(monkeypatch):
    import requests

    from musique.musicbrainz import MBClient

    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"recordings": []}

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        raise requests.ConnectionError("DNS")

    monkeypatch.setattr("time.sleep", lambda s: None)
    c = MBClient(min_interval_s=0, max_failures=2)
    monkeypatch.setattr(c.session, "get", fake_get)
    for _ in range(2):
        with pytest.raises(MBError):
            c.search_recordings("x")
    assert len(calls) == 4  # erreur réseau : 1 seul nouvel essai par requête
    assert c.disabled
    with pytest.raises(MBError, match="désactivé"):
        c.search_recordings("x")
    assert len(calls) == 4  # plus aucun appel réseau une fois coupé

    # 503 puis succès : on patiente et on réussit, le compteur d'échecs repart à zéro
    codes = iter([503, 503, 200])
    c2 = MBClient(min_interval_s=0)
    monkeypatch.setattr(c2.session, "get", lambda *a, **k: Resp(next(codes)))
    assert c2.search_recordings("x") == [] and c2.failures == 0


def test_release_ranking_order():
    def rel(status, primary, secondary=(), date="2000", fmt="CD"):
        return {"status": status, "date": date, "media": [{"format": fmt}],
                "release-group": {"primary-type": primary, "secondary-types": list(secondary)}}

    ranked = sorted([
        rel("Official", "Album", ["Compilation"], "1994"),
        rel("Bootleg", "Album", date="1990"),
        rel("Official", "Single", date="1992"),
        rel("Official", "Album", date="1993"),
        rel("Official", "Album", ["Live"], "1993"),
    ], key=release_sort_key)
    assert [(r["status"], r["release-group"]["primary-type"], r["date"]) for r in ranked[:2]] == [
        ("Official", "Album", "1993"), ("Official", "Single", "1992")]
    assert ranked[-1]["status"] == "Bootleg"


def test_choose_release_filters_track_title_variants():
    def rec(track_title, number, date):
        return {"id": "r", "title": "Creep", "releases": [{
            "id": number, "title": "Pablo Honey", "status": "Official", "date": date,
            "release-group": {"primary-type": "Album"},
            "media": [{"format": "CD", "track": [{"number": number, "title": track_title}]}]}]}

    choice = choose_release([rec("Creep (radio edit)", "13", "1993"), rec("Creep", "2", "1993")], "Creep")
    assert choice.track["number"] == "2"


def test_mistyped_late_compilation_does_not_beat_original_single():
    # Cas réel (Joy Division, 2026-10-03) : compilation de 2020 typée « Album » sans
    # « Compilation » dans MusicBrainz, face au single original de 1980.
    def rec(rid, title, primary, date):
        return {"id": rid, "title": "Love Will Tear Us Apart", "releases": [{
            "id": rid, "title": title, "status": "Official", "date": date,
            "release-group": {"primary-type": primary},
            "media": [{"format": "CD", "track": [{"number": "1", "title": "Love Will Tear Us Apart"}]}]}]}

    choice = choose_release([
        rec("a", "80s Party: Ultimate Eighties Throwback Classics", "Album", "2020"),
        rec("b", "Love Will Tear Us Apart", "Single", "1980-04"),
    ], "Love Will Tear Us Apart")
    assert choice.release["title"] == "Love Will Tear Us Apart" and choice.release["date"] == "1980-04"


def test_album_shortly_after_single_still_wins():
    def rec(rid, title, primary, date):
        return {"id": rid, "title": "Creep", "releases": [{
            "id": rid, "title": title, "status": "Official", "date": date,
            "release-group": {"primary-type": primary},
            "media": [{"format": "CD", "track": [{"number": "2", "title": "Creep"}]}]}]}

    choice = choose_release([rec("s", "Creep", "Single", "1992-09-21"), rec("a", "Pablo Honey", "Album", "1993-02-22")],
                            "Creep")
    assert choice.release["title"] == "Pablo Honey"


@pytest.mark.parametrize("track, medium, n", [
    ({"number": "2"}, {}, 2), ({"number": "A1"}, {"track-offset": 0}, 1),
    ({"number": "B5"}, {"track-offset": 9}, 10), ({"number": "x"}, {}, None)])
def test_track_number(track, medium, n):
    assert track_number(track, medium) == n


def test_prefer_case():
    assert prefer_case("SPORTS MEN", "Sports Men") == "Sports Men"
    assert prefer_case("Homework", "HOMEWORK") == "Homework"
    assert prefer_case("PHILHARMONY", "Neue Tanz") == "PHILHARMONY"  # autre album : on garde MB
    assert prefer_case(None, "x") == "x"
