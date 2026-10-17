"""Particularités de l'appareil : Termux (Android), Windows."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

log = logging.getLogger(__name__)


def is_termux() -> bool:
    return "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", "")


def fix_console_encoding() -> None:
    """La console Windows n'est pas en UTF-8 par défaut : « Beyoncé » ferait planter print."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


@contextmanager
def wake_lock(enabled: bool = True, release: bool = True) -> Iterator[None]:
    """Sous Termux, empêche Android de mettre le processeur en veille pendant un lot.

    `termux-wake-lock` fait partie de Termux (sans Termux:API). Le verrou est global à
    Termux : le relâcher à la fin relâche aussi un verrou pris à la main (d'où
    release_wake_lock).
    """
    active = enabled and is_termux() and shutil.which("termux-wake-lock") is not None
    if active:
        active = _run_quietly("termux-wake-lock")
    try:
        yield
    finally:
        if active and release and shutil.which("termux-wake-unlock"):
            _run_quietly("termux-wake-unlock")


def media_scan(paths: list[Path], enabled: bool = True) -> int:
    """Signale des fichiers à l'index des médias d'Android (MediaStore). Renvoie le nombre
    de diffusions qu'`am` a envoyées sans erreur (0 hors Termux). Ce n'est pas une preuve
    d'indexation : `am` sort avec 0 même si aucun récepteur ne reçoit la diffusion.

    Les applis qui lisent MediaStore (appli Musique d'origine, Auxio en source « Système »)
    ne voient pas les fichiers rangés par l'outil : écrits dans .musique/ (.nomedia) puis
    renommés dans la bibliothèque, ils ne sont pas indexés. Une diffusion
    MEDIA_SCANNER_SCAN_FILE par `am` (fourni par Termux) les fait apparaître ; ~0,5 s par
    fichier. Sans effet sur Auxio 4.x en source par défaut (sélecteur de fichiers), qui
    parcourt lui-même ses dossiers.

    Signaler un chemin qui n'existe plus retire son entrée de l'index (ancien nom après un
    renommage). Un échec n'est jamais bloquant : le fichier est là, seul l'affichage tarde.
    """
    if not (enabled and paths and is_termux()) or shutil.which("am") is None:
        return 0
    done = 0
    for p in paths:
        uri = "file://" + quote(Path(p).absolute().as_posix(), safe="/")
        try:
            r = subprocess.run(["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                                "-d", uri], check=False, timeout=30, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as e:
            log.info("indexation Android impossible (%s) : %s", e, p)
            continue
        if r.returncode == 0:
            done += 1
        else:
            log.info("indexation Android refusée (%s) : %s", (r.stderr or r.stdout).strip()[:200], p)
    log.debug("MediaStore : %d/%d diffusion(s) envoyée(s)", done, len(paths))
    return done


def _run_quietly(cmd: str) -> bool:
    """Le wake-lock est un confort : s'il échoue ou bloque, on continue sans."""
    try:
        subprocess.run([cmd], check=False, timeout=30, capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("%s a échoué (%s) : on continue sans", cmd, e)
        return False
    log.debug("%s : ok", cmd)
    return True
