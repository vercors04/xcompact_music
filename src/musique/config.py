"""Configuration : un fichier TOML par appareil, avec des valeurs par défaut sûres.

Emplacement du fichier (premier trouvé) :
  1. option --config
  2. variable d'environnement MUSIQUE_CONFIG
  3. Windows : %APPDATA%\\musique\\config.toml
     ailleurs : $XDG_CONFIG_HOME/musique/config.toml (défaut ~/.config/musique/config.toml)

La variable MUSIQUE_LIBRARY surcharge `library` (pratique pour tester).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from musique.matching import MatchConfig


def _default_config_path() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "musique" / "config.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "musique" / "config.toml"


def _default_state_dir() -> Path:
    """Cache, index, rapports, logs : local à l'appareil, jamais synchronisé."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "musique"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "musique"


@dataclass
class LoudnessConfig:
    target_lufs: float = -18.0  # référence ReplayGain 2.0
    peak_ceiling_dbfs: float = -1.0
    cap_positive_gain: bool = True
    true_peak: bool = False  # True = plus juste mais ×7 plus lent sur téléphone


@dataclass
class CoverConfig:
    size: int = 600  # px ; largement suffisant pour un écran de 720 px
    quality: int = 70  # qualité JPEG demandée au serveur (≈ 40-120 Ko à 600 px)


@dataclass
class MetadataConfig:
    musicbrainz: bool = True  # album original, année, n° de piste, pochette Cover Art Archive
    contact: str = ""  # optionnel : e-mail ou URL ajouté au User-Agent (demandé par MusicBrainz)


@dataclass
class DownloadConfig:
    sleep_between_s: float = 2.0  # politesse envers YouTube, limite le risque de blocage
    retries: int = 3
    js_runtime: str = ""  # "" = défaut de yt-dlp (deno) ; ex. "node" ou "deno:/chemin/deno"
    cookies_file: str = ""  # optionnel, si YouTube demande « Sign in to confirm you're not a bot »


@dataclass
class Config:
    library: Path
    state_dir: Path = field(default_factory=_default_state_dir)
    layout: str = "flat"  # "flat" : tout à la racine ; "artist_album" : Artiste/Album/NN - Titre
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    sources: list[str] = field(default_factory=lambda: ["ytmusic"])
    search_limit: int = 10
    wake_lock: bool = True  # Termux : empêche la mise en veille pendant un lot
    release_wake_lock: bool = True
    matching: MatchConfig = field(default_factory=MatchConfig)
    loudness: LoudnessConfig = field(default_factory=LoudnessConfig)
    cover: CoverConfig = field(default_factory=CoverConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    config_path: Path | None = None


class ConfigError(ValueError):
    pass


def _build(cls, data: dict, section: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{section}] clé(s) inconnue(s) : {', '.join(sorted(unknown))}")
    return cls(**data)


def load(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("MUSIQUE_CONFIG") or _default_config_path()).expanduser()
    data: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)
    elif path:
        raise ConfigError(f"fichier de configuration introuvable : {cfg_path}")

    library = os.environ.get("MUSIQUE_LIBRARY") or data.pop("library", None)
    data.pop("library", None)
    if not library:
        raise ConfigError(
            f"aucune bibliothèque configurée. Crée {cfg_path} avec par exemple :\n"
            f'  library = "/storage/XXXX-XXXX/Music"\n'
            f"(voir config.example.toml)"
        )

    sections = {
        "matching": MatchConfig,
        "loudness": LoudnessConfig,
        "cover": CoverConfig,
        "metadata": MetadataConfig,
        "download": DownloadConfig,
    }
    kwargs: dict = {}
    for name, cls in sections.items():
        if name in data:
            kwargs[name] = _build(cls, data.pop(name), name)
    if "state_dir" in data:
        kwargs["state_dir"] = Path(data.pop("state_dir")).expanduser()
    top = {f.name for f in fields(Config)} - set(sections) - {"library", "state_dir", "config_path"}
    unknown = set(data) - top
    if unknown:
        raise ConfigError(f"clé(s) inconnue(s) : {', '.join(sorted(unknown))}")
    kwargs.update(data)
    cfg = Config(library=Path(library).expanduser(), config_path=cfg_path if cfg_path.exists() else None, **kwargs)
    if not 0 < cfg.matching.doubtful <= cfg.matching.accept <= 1:
        raise ConfigError("il faut 0 < matching.doubtful ≤ matching.accept ≤ 1")
    if cfg.layout not in ("flat", "artist_album"):
        raise ConfigError(f'layout = "{cfg.layout}" inconnu (possibles : "flat", "artist_album")')
    return cfg
