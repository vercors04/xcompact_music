from pathlib import Path

import pytest

from musique.config import ConfigError, load

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def test_example_config_loads(monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    cfg = load(EXAMPLE)
    assert cfg.library == Path("/storage/C1B3-16F0/Music")
    assert cfg.loudness.target_lufs == -18.0
    assert cfg.matching.accept == 0.85
    assert cfg.sources == ["ytmusic"]


def test_sections_and_env_override(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('library = "/a"\n[matching]\naccept = 0.9\n[loudness]\ntrue_peak = true\n', encoding="utf-8")
    monkeypatch.setenv("MUSIQUE_LIBRARY", str(tmp_path))
    cfg = load(p)
    assert cfg.library == tmp_path  # la variable d'environnement l'emporte
    assert cfg.matching.accept == 0.9 and cfg.matching.doubtful == 0.6
    assert cfg.loudness.true_peak is True


def test_unknown_key_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('library = "/a"\n[loudness]\ntarget = -14\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="target"):
        load(p)


def test_missing_library_is_explained(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="bibliothèque"):
        load(p)


def test_toml_syntax_error_is_explained(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('library = "/a\n', encoding="utf-8")  # guillemet oublié
    with pytest.raises(ConfigError, match="syntaxe TOML"):
        load(p)


@pytest.mark.parametrize("toml, where", [
    ('library = "/a"\n[matching]\naccept = "haut"\n', "accept"),
    ('library = "/a"\nwake_lock = "oui"\n', "wake_lock"),
    ('library = "/a"\nsearch_limit = 2.5\n', "search_limit"),
    ('library = "/a"\nsources = "ytmusic"\n', "sources"),
    ("library = 42\n", "library"),
    ('library = "/a"\nmatching = 3\n', "matching"),
])
def test_wrong_types_are_rejected(tmp_path, monkeypatch, toml, where):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError, match=where):
        load(p)


def test_integer_accepted_for_float(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('library = "/a"\n[loudness]\ntarget_lufs = -16\n', encoding="utf-8")
    cfg = load(p)
    assert cfg.loudness.target_lufs == -16.0 and isinstance(cfg.loudness.target_lufs, float)


def test_inconsistent_thresholds(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIQUE_LIBRARY", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('library = "/a"\n[matching]\naccept = 0.5\ndoubtful = 0.7\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load(p)
