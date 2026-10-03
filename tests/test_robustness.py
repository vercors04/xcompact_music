"""Robustesse dans la durée : écriture sûre, fichiers d'état abîmés, erreurs, limites."""

import json

import pytest

from musique.config import ConfigError, load
from musique.library import LibraryIndex, rewrite_atomically, run_lock
from musique.models import Candidate, Outcome, Quality, Query
from musique.report import KEEP_REPORTS, write_tsv


# --------------------------------------------------------------------------- #
# Réécriture d'un fichier déjà rangé (gain, retag)
# --------------------------------------------------------------------------- #


def test_rewrite_atomically_replaces_content(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "a.opus"
    f.write_bytes(b"avant")
    rewrite_atomically(f, lib, lambda p: p.write_bytes(b"apres"))
    assert f.read_bytes() == b"apres"
    assert list((lib / ".musique" / "tmp").iterdir()) == []  # aucune copie laissée


def test_rewrite_atomically_keeps_original_on_failure(tmp_path):
    """Interruption au milieu de l'écriture des tags : l'original doit rester intact."""
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "a.opus"
    f.write_bytes(b"original")

    def crash(p):
        p.write_bytes(b"a moitie ecr")
        raise KeyboardInterrupt  # comme Ctrl+C ou Android qui tue le processus

    with pytest.raises(KeyboardInterrupt):
        rewrite_atomically(f, lib, crash)
    assert f.read_bytes() == b"original"
    assert list((lib / ".musique" / "tmp").iterdir()) == []


def test_rewrite_atomically_outside_library(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = tmp_path / "ailleurs.opus"
    f.write_bytes(b"x")
    rewrite_atomically(f, lib, lambda p: p.write_bytes(b"y"))
    assert f.read_bytes() == b"y"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ailleurs.opus", "lib"]


# --------------------------------------------------------------------------- #
# Fichiers d'état abîmés : jamais bloquants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("content", [
    "pas du json", "[]", '{"version": 1, "library": "LIB", "entries": {"x": {"inconnu": 1}}}',
    '{"version": 1, "library": "LIB", "entries": "nimporte quoi"}',
])
def test_corrupted_index_is_rebuilt(tmp_path, content):
    lib, state = tmp_path / "lib", tmp_path / "state"
    lib.mkdir()
    state.mkdir()
    (state / "index.json").write_text(content.replace("LIB", str(lib).replace("\\", "\\\\")), encoding="utf-8")
    idx = LibraryIndex(lib, state)  # ne doit pas lever
    assert idx.entries == {} and idx.queries == {}
    (lib / "x.opus").write_bytes(b"x")
    assert idx.refresh() == (1, 0)


def test_pending_keys_are_recomputed(tmp_path):
    """Clés d'une ancienne version (ou écrites à la main) : pas de doublon fantôme."""
    from musique.pipeline import PendingStore

    entry = {"raw": "Radiohead - Creep (Live)", "artist": "Radiohead", "title": "Creep (Live)"}
    (tmp_path / "pending.json").write_text(json.dumps({"ancienne-cle": entry}), encoding="utf-8")
    store = PendingStore(tmp_path)
    assert list(store.items) == ["radiohead|creep live"]
    store.add(Query(**{k: entry[k] for k in ("raw", "artist", "title")}), [])
    assert len(store.items) == 1


def test_query_memory_survives_index_version_change(tmp_path):
    """Sinon une requête au résultat instable (classique) est retéléchargée en double."""
    lib, state = tmp_path / "lib", tmp_path / "state"
    lib.mkdir()
    state.mkdir()
    (lib / "Arvo Pärt - Spiegel im Spiegel.opus").write_bytes(b"x")
    old = {"version": LibraryIndex.VERSION - 1, "library": str(lib), "entries": {"obsolète": {"vieux": 1}},
           "queries": {"arvo part|spiegel im spiegel": "Arvo Pärt - Spiegel im Spiegel.opus",
                       "disparu|x": "n'existe plus.opus"}}
    (state / "index.json").write_text(json.dumps(old), encoding="utf-8")
    idx = LibraryIndex(lib, state)
    idx.refresh()
    assert idx.by_query("arvo part|spiegel im spiegel") == "Arvo Pärt - Spiegel im Spiegel.opus"
    assert "disparu|x" not in idx.queries


@pytest.mark.parametrize("content", ["pas du json", "[1, 2]", '{"k": "pas un dict", "k2": {"sans": "raw"}}'])
def test_corrupted_pending_is_ignored(tmp_path, content):
    from musique.pipeline import PendingStore

    (tmp_path / "pending.json").write_text(content, encoding="utf-8")
    assert PendingStore(tmp_path).queries() == []


# --------------------------------------------------------------------------- #
# Pipeline : erreurs propres, douteux nettoyés
# --------------------------------------------------------------------------- #


class FakeSource:
    name = "ytmusic"

    def __init__(self, error=None, candidates=()):
        self.error, self.candidates = error, list(candidates)

    def search(self, query, limit):
        if self.error:
            raise self.error
        return self.candidates


def make_ctx(tmp_path, source, dry_run=False):
    from musique.pipeline import Context, PendingStore

    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = load_cfg(tmp_path, lib)
    return Context(cfg=cfg, sources=[source], index=LibraryIndex(lib, cfg.state_dir), dry_run=dry_run,
                   pending=PendingStore(cfg.state_dir))


def load_cfg(tmp_path, lib):
    p = tmp_path / "c.toml"
    p.write_text(f"library = '{lib}'\nstate_dir = '{tmp_path / 'state'}'\n", encoding="utf-8")
    return load(p)


def test_disk_error_becomes_clean_error_result(tmp_path, monkeypatch):
    from musique.pipeline import process

    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    ctx = make_ctx(tmp_path, FakeSource(error=OSError(28, "No space left on device")))
    r = process(Query("A - B", "A", "B"), ctx)
    assert r.outcome is Outcome.ERROR and "No space left" in r.message and "inattendue" not in r.message


def test_doubtful_then_present_leaves_pending(tmp_path, monkeypatch):
    """Une requête douteuse qui finit par être présente sort de pending.json."""
    from musique.pipeline import process

    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    q = Query("Radiohead - Creep", "Radiohead", "Creep")
    doubtful = Candidate("ytmusic", "v1", "Creep (Live)", ["Radiohead"], quality=Quality("opus", 160))
    ctx = make_ctx(tmp_path, FakeSource(candidates=[doubtful]))
    assert process(q, ctx).outcome is Outcome.DOUBTFUL
    assert len(ctx.pending.items) == 1

    # Plus tard, le morceau est dans la bibliothèque (téléchargé autrement) :
    f = ctx.cfg.library / "Radiohead - Creep.opus"
    f.write_bytes(b"x")
    ctx.index.note_existing(f, "radiohead|creep")
    assert process(q, ctx).outcome is Outcome.PRESENT
    assert ctx.pending.items == {}


def test_dry_run_never_writes_pending(tmp_path, monkeypatch):
    from musique.pipeline import process

    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    doubtful = Candidate("ytmusic", "v1", "Creep (Live)", ["Radiohead"], quality=Quality("opus", 160))
    ctx = make_ctx(tmp_path, FakeSource(candidates=[doubtful]), dry_run=True)
    assert process(Query("Radiohead - Creep", "Radiohead", "Creep"), ctx).outcome is Outcome.DOUBTFUL
    assert not (ctx.cfg.state_dir / "pending.json").exists()


# --------------------------------------------------------------------------- #
# Ligne de commande : codes de sortie et messages, jamais de trace Python
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    p = tmp_path / "c.toml"
    p.write_text(f"library = '{lib}'\nstate_dir = '{tmp_path / 'state'}'\n", encoding="utf-8")
    return p


def test_missing_query_file_is_usage_error(cli_config, capsys):
    from musique.cli import main

    assert main(["get", "-f", "nexiste_pas.txt", "--config", str(cli_config)]) == 2
    assert "illisible" in capsys.readouterr().err


def test_second_instance_is_refused(cli_config, tmp_path, capsys):
    from musique.cli import main

    with run_lock(tmp_path / "state"):  # une autre instance tient le verrou
        assert main(["get", "A - B", "--config", str(cli_config)]) == 3
    assert "déjà" in capsys.readouterr().err


def test_console_never_shows_traceback():
    import logging

    from musique.cli import _ConsoleFormatter

    try:
        raise KeyError("détail interne")
    except KeyError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "message clair", None, __import__("sys").exc_info())
    file_text = logging.Formatter("%(message)s").format(record)  # le journal : trace complète
    console_text = _ConsoleFormatter("%(message)s").format(record)
    assert "Traceback" in file_text
    assert console_text == "message clair"
    assert record.exc_info is not None  # rien de perdu pour les autres handlers


def test_unknown_source_rejected_at_load(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text("library = '/a'\nsources = ['spotify']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="spotify"):
        load(p)


# --------------------------------------------------------------------------- #
# Croissance dans le temps
# --------------------------------------------------------------------------- #


def test_reports_are_pruned(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    for i in range(KEEP_REPORTS + 20):
        (reports / f"20200101-{i:06d}.tsv").write_text("x")
    newest = write_tsv([], reports)
    remaining = sorted(reports.glob("*.tsv"))
    assert len(remaining) == KEEP_REPORTS and newest in remaining
    assert remaining[0].name == "20200101-000021.tsv"  # les plus anciens ont été supprimés


def test_musicbrainz_bad_request_does_not_trip_breaker(monkeypatch):
    from musique.musicbrainz import MBClient, MBRequestError

    class Resp:
        status_code = 400

    c = MBClient(min_interval_s=0, max_failures=2)
    monkeypatch.setattr(c.session, "get", lambda *a, **k: Resp())
    for _ in range(5):
        with pytest.raises(MBRequestError):
            c.search_recordings('recording:"bizarre"')
    assert not c.disabled  # 5 requêtes refusées ≠ serveur en panne
