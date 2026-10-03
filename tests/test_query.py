from musique.query import collect, parse_query, query_key, read_lines


def test_parse_artist_title():
    q = parse_query("Daft Punk - Around the World")
    assert (q.artist, q.title) == ("Daft Punk", "Around the World")


def test_parse_en_dash_and_extra_dashes():
    q = parse_query("Daft Punk – Around the World - Radio Edit")
    assert (q.artist, q.title) == ("Daft Punk", "Around the World - Radio Edit")


def test_hyphen_inside_name_is_not_a_separator():
    q = parse_query("Jay-Z")
    assert q.artist is None and q.raw == "Jay-Z"
    q = parse_query("AC-DC - Thunderstruck")
    assert (q.artist, q.title) == ("AC-DC", "Thunderstruck")


def test_free_text_comments_and_blanks():
    assert parse_query("radiohead creep").artist is None
    assert parse_query("   ") is None
    assert parse_query("# commentaire") is None


def test_key_is_normalised():
    assert query_key(parse_query("Beyoncé - Halo")) == query_key(parse_query("beyonce  -  HALO"))


def test_collect_deduplicates_preserving_order():
    qs = collect(["B - x", "A - y", "b - X", "# z", ""])
    assert [q.raw for q in qs] == ["B - x", "A - y"]


def test_read_lines_handles_bom_and_crlf(tmp_path):
    p = tmp_path / "liste.txt"
    p.write_bytes("﻿Daft Punk - One More Time\r\nRadiohead - Creep\r\n".encode("utf-8"))
    qs = collect(read_lines(p))
    assert [q.artist for q in qs] == ["Daft Punk", "Radiohead"]
