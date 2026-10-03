import pytest

from musique.textnorm import (
    artist_similarity,
    fold,
    is_live_album,
    similarity,
    split_artists,
    split_title,
    variant_counts,
    variant_tags,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Beyoncé", "beyonce"),
        ("Sigur Rós", "sigur ros"),
        ("Don’t Stop Me Now", "dont stop me now"),
        ("AC/DC", "ac dc"),
        ("Simon & Garfunkel", "simon and garfunkel"),
        ("  Hello,   World!! ", "hello world"),
        ("Motörhead", "motorhead"),
        ("Röyksopp", "royksopp"),
        ("Røyksopp", "royksopp"),
        ("Mø", "mo"),
        ("Kino - Группа крови", "kino gruppa krovi"),  # cyrillique translittéré
        ("Ёжик", "ezhik"),
        ("細野晴臣", "細野晴臣"),
        ("Σωκράτης", "σωκρατησ"),  # casefold : sigma final ς → σ
        ("snake_case", "snake case"),
    ],
)
def test_fold(raw, expected):
    assert fold(raw) == expected


def test_non_latin_titles_are_not_all_identical():
    # Avant correction, tout ce qui n'était pas [a-z0-9] disparaissait : 1.0 partout.
    assert similarity("Группа крови", "Звезда по имени Солнце") < 0.5
    assert similarity("Группа крови", "группа КРОВИ") == 1.0
    assert similarity("Gruppa krovi", "Группа крови") == 1.0  # tapé en alphabet latin
    assert similarity("スポーツマン", "ライディーン") < 0.5


def test_punctuation_only_names():
    assert similarity("!!!", "!!!") == 1.0
    assert similarity("!!!", "???") == 0.0


def test_non_latin_query_keys_are_distinct():
    from musique.query import parse_query, query_key

    assert query_key(parse_query("Кино - Группа крови")) != query_key(parse_query("ДДТ - Осень"))


@pytest.mark.parametrize(
    "title, core, quals",
    [
        ("Around the World", "Around the World", []),
        ("Around the World (Radio Edit)", "Around the World", ["Radio Edit"]),
        ("Creep - Acoustic", "Creep", ["Acoustic"]),
        ("Get Lucky (feat. Pharrell Williams)", "Get Lucky", ["feat. Pharrell Williams"]),
        ("Get Lucky feat. Pharrell Williams", "Get Lucky", ["feat. Pharrell Williams"]),
        ("Hey Jude - Remastered 2015", "Hey Jude", ["Remastered 2015"]),
        ("Song [Live] (2011 Remaster)", "Song", ["Live", "2011 Remaster"]),
        ("(Untitled)", "(Untitled)", []),
        ("Jay-Z", "Jay-Z", []),  # tiret sans espaces : pas un séparateur
    ],
)
def test_split_title(title, core, quals):
    assert split_title(title) == (core, quals)


@pytest.mark.parametrize(
    "text, tags",
    [
        ("Around the World (Radio Edit)", {"edit"}),
        ("Creep (Acoustic)", {"acoustic"}),
        ("One More Time (Live at Wembley)", {"live"}),
        ("Strobe (Original Mix)", set()),  # « original mix » = version originale
        ("Hey Jude - Remastered 2015", set()),
        ("Blinding Lights (Sped Up)", {"speed"}),
        ("Halo (Karaoke Version)", {"instrumental"}),
        ("Hello (Album Version)", set()),
        ("Oliver Twist", set()),  # « live » à l'intérieur d'un mot : pas une variante
        ("Titanium (David Guetta Remix)", {"remix"}),
    ],
)
def test_variant_tags(text, tags):
    assert variant_tags(text) == tags


def test_variant_counts_distinguishes_title_word_from_qualifier():
    assert variant_counts("Live Forever")["live"] == 1
    assert variant_counts("Live Forever (Live at Knebworth)")["live"] == 2


@pytest.mark.parametrize(
    "album, live",
    [
        ("Live at Wembley Stadium", True),
        ("MTV Unplugged in New York", True),
        ("Live Through This", False),  # album studio de Hole
        ("Alive 2007", False),  # non détectable par le nom : assumé
        ("Homework", False),
        (None, False),
    ],
)
def test_is_live_album(album, live):
    assert is_live_album(album) is live


def test_split_artists():
    assert split_artists("Daft Punk feat. Pharrell Williams") == ["Daft Punk", "Pharrell Williams"]
    assert split_artists("A, B & C") == ["A", "B", "C"]
    assert split_artists("Jay-Z") == ["Jay-Z"]


def test_similarity_word_order_and_inclusion():
    assert similarity("around the world", "World Around The") == 1.0
    assert similarity("Love", "Love Me Do") < 0.6  # inclusion ≠ identité
    assert similarity("Beyoncé", "beyonce") == 1.0


@pytest.mark.parametrize(
    "query, cands, low, high",
    [
        ("Daft Punk", ["Daft Punk"], 1.0, 1.0),
        ("Daft Punk", ["Daft Punk", "Pharrell Williams"], 1.0, 1.0),
        ("The Beatles", ["Beatles"], 1.0, 1.0),
        ("Simon & Garfunkel", ["Simon & Garfunkel"], 1.0, 1.0),
        ("Daft Punk feat. Pharrell", ["Daft Punk", "Pharrell Williams"], 0.8, 1.0),
        ("Radiohead", ["Postmodern Jukebox"], 0.0, 0.35),
        ("Radiohead", [], 0.0, 0.0),
    ],
)
def test_artist_similarity(query, cands, low, high):
    assert low <= artist_similarity(query, cands) <= high
