"""Inspection des fichiers (ffprobe) et téléchargement des pochettes."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    duration: float | None
    sample_rate: int | None
    channels: int | None
    bitrate_kbps: float | None  # débit moyen du fichier (taille / durée)


def probe(path: Path, ffprobe: str = "ffprobe") -> AudioInfo:
    """Codec, durée, fréquence d'échantillonnage du premier flux audio.

    Sert à vérifier qu'on a bien obtenu ce qu'on croit (codec attendu, pas de
    réencodage, durée cohérente avec la recherche).
    """
    cmd = [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
           "stream=codec_name,sample_rate,channels:format=duration,size", "-of", "json", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"ffprobe a échoué : {e}") from e
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe a échoué : {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    size = int(fmt["size"]) if fmt.get("size") else None
    kbps = size * 8 / duration / 1000 if size and duration else None
    return AudioInfo(
        codec=stream.get("codec_name", "?"),
        duration=duration,
        sample_rate=int(stream["sample_rate"]) if stream.get("sample_rate") else None,
        channels=stream.get("channels"),
        bitrate_kbps=kbps,
    )


def fetch_cover(url: str, timeout: float = 20.0) -> bytes | None:
    """Télécharge une pochette. Un échec n'est jamais fatal (on tague sans pochette)."""
    import requests

    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        log.info("pochette non récupérée (%s) : %s", url, e)
        return None
    data = r.content
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        log.info("pochette ignorée : ni JPEG ni PNG (%s)", url)
        return None
    return data
