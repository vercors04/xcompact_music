"""Particularités de l'appareil : Termux (Android), Windows."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator

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

    `termux-wake-lock` fait partie de Termux (pas besoin de Termux:API). Attention :
    le verrou est global à l'appli Termux ; le relâcher à la fin relâche aussi un
    verrou que tu aurais pris toi-même (d'où l'option release_wake_lock).
    """
    active = enabled and is_termux() and shutil.which("termux-wake-lock") is not None
    if active:
        subprocess.run(["termux-wake-lock"], check=False, timeout=30)
        log.debug("wake-lock pris")
    try:
        yield
    finally:
        if active and release and shutil.which("termux-wake-unlock"):
            subprocess.run(["termux-wake-unlock"], check=False, timeout=30)
            log.debug("wake-lock relâché")
