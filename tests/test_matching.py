"""Cas réels : candidats renvoyés par YouTube Music (filter="songs") le 2026-10-02."""

import pytest

from musique.matching import MatchConfig, decide, duration_factor, rank, score_candidate
from musique.models import Candidate, Decision, Quality, Query


def cand(title, artists, album=None, duration=None, rank=0, explicit=None, vid=None):
    return Candidate("ytmusic", vid or title, title, artists, album=album, duration=duration, rank=rank,
                     quality=Quality("opus", 160), explicit=explicit)


DAFT = [
    cand("Around the World", ["Daft Punk"], "Homework", 430, 0),
    cand("Around the World (Radio Edit)", ["Daft Punk"], "Around the World", 242, 1),
    cand("Something About Us", ["Daft Punk"], "Something About Us", 233, 2),
    cand("One More Time", ["Daft Punk"], "Discovery", 321, 3),
    cand("Around the World / Harder, Better, Faster, Stronger", ["Daft Punk"], "Alive 2007", 343, 4),
]

RADIOHEAD = [
    cand("Creep", ["Radiohead"], "Creep", 239, 0),
    cand("Creep (Acoustic)", ["Radiohead"], "Creep EP", 259, 1),
    cand("No Surprises", ["Radiohead"], "OK Computer OKNOTOK 1997 2017", 229, 2),
]


def test_exact_match_wins_and_is_accepted():
    q = Query("Daft Punk - Around the World", "Daft Punk", "Around the World")
    r = rank(q, DAFT)
    assert r[0].candidate.album == "Homework"
    assert r[0].score > 0.95
    assert decide(r[0]) is Decision.ACCEPT


def test_unwanted_variant_falls_below_accept():
    q = Query("Daft Punk - Around the World", "Daft Punk", "Around the World")
    remix = score_candidate(q, cand("Around the World (Mellow Mix)", ["Daft Punk"], "Homework (25th Anniversary)", 472))
    # titre et artiste parfaits, mais un remix non demandé : ×0.65
    assert remix.score == pytest.approx(0.65)
    assert decide(remix) is Decision.DOUBTFUL


def test_edit_is_a_soft_variant():
    q = Query("Daft Punk - Around the World", "Daft Punk", "Around the World")
    r = rank(q, DAFT)
    assert r[0].candidate.album == "Homework"  # la version longue gagne quand elle existe
    edit = next(s for s in r if "Radio Edit" in s.candidate.title)
    assert edit.score == pytest.approx(0.88 * 0.99)


def test_only_edits_available_is_accepted():
    # Cas réel (2026-10-02) : toutes les versions officielles s'appellent « Radio Edit ».
    q = Query("Stromae - Alors on danse", "Stromae", "Alors on danse")
    cands = [
        cand("Alors on danse (Radio Edit)", ["Stromae"], "Alors On Danse", 208, 0),
        cand("Alors on danse (Radio Edit)", ["Stromae"], "Cheese", 207, 1),
        cand("Alors On Danse", ["Alors On Danse"], "Alors on danse", 205, 8),
        cand("Alors On Danse", ["Te Pai"], "Alors On Danse", 140, 2),
    ]
    r = rank(q, cands)
    assert r[0].candidate.artists == ["Stromae"]
    assert decide(r[0]) is Decision.ACCEPT
    assert decide(score_candidate(q, cands[3])) is Decision.REJECT  # homonyme d'un autre artiste


def test_requested_variant_is_preferred():
    q = Query("Radiohead - Creep (Acoustic)", "Radiohead", "Creep (Acoustic)")
    r = rank(q, RADIOHEAD)
    assert r[0].candidate.title == "Creep (Acoustic)"
    assert decide(r[0]) is Decision.ACCEPT


def test_studio_version_preferred_over_acoustic():
    q = Query("Radiohead - Creep", "Radiohead", "Creep")
    r = rank(q, RADIOHEAD)
    assert r[0].candidate.title == "Creep"
    assert r[1].candidate.title == "Creep (Acoustic)"
    assert r[1].score < MatchConfig().accept


def test_cover_by_other_artist_is_rejected():
    q = Query("Radiohead - Creep", "Radiohead", "Creep")
    cover = cand("Creep", ["Postmodern Jukebox", "Haley Reinhart"], "Creep", 270)
    s = score_candidate(q, cover)
    assert decide(s) is Decision.REJECT, s


def test_different_song_same_artist_is_not_accepted():
    q = Query("Radiohead - Creep", "Radiohead", "Creep")
    s = score_candidate(q, RADIOHEAD[2])  # No Surprises
    assert decide(s) is Decision.REJECT


def test_live_album_counts_as_live_variant():
    q = Query("Daft Punk - One More Time", "Daft Punk", "One More Time")
    studio = cand("One More Time", ["Daft Punk"], "Discovery", 320)
    live = cand("One More Time", ["Daft Punk"], "Live at Wembley", 330, rank=0)
    assert score_candidate(q, studio).score > score_candidate(q, live).score
    assert decide(score_candidate(q, live)) is not Decision.ACCEPT


def test_title_containing_variant_word_is_not_penalised():
    q = Query("Oasis - Live Forever", "Oasis", "Live Forever")
    studio = cand("Live Forever", ["Oasis"], "Definitely Maybe", 276)
    live = cand("Live Forever (Live at Knebworth)", ["Oasis"], "Knebworth 1996", 290)
    assert decide(score_candidate(q, studio)) is Decision.ACCEPT
    assert decide(score_candidate(q, live)) is not Decision.ACCEPT


def test_unrequested_guest_loses_to_solo_version():
    # Cas réel (2026-10-03) : YouTube Music classe en tête la version avec Kanye West.
    q = Query("Stromae - Alors on danse", "Stromae", "Alors on danse")
    solo = cand("Alors on danse (Radio Edit)", ["Stromae"], "Alors On Danse", 208, 2)
    # Tel que YouTube Music le présente : Kanye West dans le titre seulement…
    kanye = cand("Alors On Danse (feat. Kanye West)", ["Stromae"], "Alors On Danse", 215, 0)
    r = rank(q, [kanye, solo])
    assert r[0].candidate is solo and decide(r[0]) is Decision.ACCEPT
    # … ou crédité comme artiste : même résultat.
    kanye2 = cand("Alors On Danse (feat. Kanye West)", ["Stromae", "Kanye West"], "Alors On Danse", 215, 0)
    assert rank(q, [kanye2, solo])[0].candidate is solo


def test_featuring_in_title_only_is_not_a_guest():
    # Cas réel (2026-10-03) : l'original est « Angel (feat. Horace Andy) », crédité
    # [Massive Attack] ; « Angel (Angel Dust) » est un remix au nom non reconnu.
    q = Query("Massive Attack - Angel", "Massive Attack", "Angel")
    original = cand("Angel (feat. Horace Andy)", ["Massive Attack"], "Mezzanine", 380, 0)
    remix = cand("Angel (Angel Dust)", ["Massive Attack"], "Angel (Angel Dust)", 364, 7)
    assert rank(q, [remix, original])[0].candidate is original


def test_transliterated_cyrillic_query():
    q = Query("Kino - Gruppa krovi", "Kino", "Gruppa krovi")
    s = score_candidate(q, cand("Группа крови", ["Кино"], "Группа крови", 285))
    assert decide(s) is Decision.ACCEPT


def test_guest_only_version_is_still_accepted():
    q = Query("Daft Punk - Get Lucky", "Daft Punk", "Get Lucky")
    s = score_candidate(q, cand("Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
                                ["Daft Punk", "Pharrell Williams", "Nile Rodgers"], "Random Access Memories", 369))
    assert decide(s) is Decision.ACCEPT


def test_requested_guest_is_not_penalised():
    q = Query("Daft Punk feat. Pharrell - Get Lucky", "Daft Punk feat. Pharrell", "Get Lucky")
    s = score_candidate(q, cand("Get Lucky", ["Daft Punk", "Pharrell Williams"], "Random Access Memories", 369))
    assert s.details["variants"] == 1.0


def test_known_duration_discriminates_versions():
    # Requête venant d'une playlist : la durée attendue est connue (430 s).
    q = Query("Daft Punk - Around the World", "Daft Punk", "Around the World", duration=430)
    album = cand("Around the World", ["Daft Punk"], "Homework", 430)
    other = cand("Around the World", ["Daft Punk"], "Musique Vol. 1", 238)  # même titre, version courte
    assert decide(score_candidate(q, album)) is Decision.ACCEPT
    assert decide(score_candidate(q, other)) is Decision.REJECT


def test_duration_factor_shape():
    cfg = MatchConfig()
    assert duration_factor(None, 200, cfg) == 1.0
    assert duration_factor(200, 202, cfg) == 1.0
    # 13 s d'écart : 10 s au-delà de la tolérance de 3 s → 1 - 10/30
    assert duration_factor(200, 213, cfg) == pytest.approx(1 - 10 / 30)
    assert duration_factor(200, 400, cfg) == cfg.duration_floor


def test_free_text_query():
    q = Query("radiohead creep")
    r = rank(q, RADIOHEAD)
    assert r[0].candidate.title == "Creep"
    assert decide(r[0]) is Decision.ACCEPT


def test_free_text_query_with_variant():
    q = Query("radiohead creep acoustic")
    r = rank(q, RADIOHEAD)
    assert r[0].candidate.title == "Creep (Acoustic)"
    assert decide(r[0]) is Decision.ACCEPT
    assert decide(score_candidate(q, RADIOHEAD[0])) is not Decision.ACCEPT


def test_accents_and_case_do_not_matter():
    q = Query("beyonce - halo", "beyonce", "halo")
    s = score_candidate(q, cand("Halo", ["Beyoncé"], "I Am... Sasha Fierce", 261))
    assert s.score > 0.95


def test_tie_prefers_explicit_version():
    q = Query("Eminem - Lose Yourself", "Eminem", "Lose Yourself")
    clean = cand("Lose Yourself", ["Eminem"], "8 Mile", 326, rank=0, explicit=False, vid="clean")
    explicit = cand("Lose Yourself", ["Eminem"], "8 Mile", 326, rank=1, explicit=True, vid="explicit")
    assert rank(q, [clean, explicit])[0].candidate.id == "explicit"


def test_no_candidates_is_reject():
    assert decide(None) is Decision.REJECT
