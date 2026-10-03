"""Registre des sources disponibles."""

from __future__ import annotations

from musique.config import Config
from musique.sources.base import Source


def build_sources(cfg: Config) -> list[Source]:
    """Instancie les sources listées dans la config, dans l'ordre de priorité."""
    out: list[Source] = []
    for name in cfg.sources:
        if name == "ytmusic":
            from musique.sources.ytmusic import YTMusicSource

            out.append(YTMusicSource(cfg))
        else:
            raise ValueError(f"source inconnue : {name!r} (disponibles : ytmusic)")
    return out
