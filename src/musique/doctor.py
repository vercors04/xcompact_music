"""`musique doctor` : vérifie tout ce dont l'outil a besoin, avec un diagnostic clair."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import uuid

from musique.config import Config
from musique.device import is_termux

OK, WARN, FAIL = "ok  ", "ATTN", "FAIL"


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return p.returncode, (p.stdout + p.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def _tool_version(cmd: str, args: list[str], pattern: str) -> str | None:
    if not shutil.which(cmd):
        return None
    _, out = _run([cmd, *args])
    m = re.search(pattern, out)
    return m.group(1) if m else "?"


def run_checks(cfg: Config | None, args) -> int:
    results: list[tuple[str, str, str]] = []

    def add(status: str, what: str, detail: str = "") -> None:
        results.append((status, what, detail))

    # Python
    add(OK if sys.version_info >= (3, 10) else FAIL, "Python", sys.version.split()[0])

    # ffmpeg / ffprobe et le filtre ebur128
    ffmpeg = cfg.ffmpeg if cfg else "ffmpeg"
    v = _tool_version(ffmpeg, ["-hide_banner", "-version"], r"ffmpeg version (\S+)")
    if v is None:
        add(FAIL, "ffmpeg", "absent. Termux : pkg install ffmpeg")
    else:
        _, filters = _run([ffmpeg, "-hide_banner", "-filters"])
        add(OK if "ebur128" in filters else FAIL, "ffmpeg", f"{v}" + ("" if "ebur128" in filters else " SANS ebur128"))
    ffprobe = cfg.ffprobe if cfg else "ffprobe"
    v = _tool_version(ffprobe, ["-hide_banner", "-version"], r"ffprobe version (\S+)")
    add(OK if v else FAIL, "ffprobe", v or "absent. Termux : pkg install ffmpeg")

    # Runtime JavaScript pour yt-dlp
    deno = _tool_version("deno", ["--version"], r"deno (\d+\.\d+\.\d+)")
    custom = cfg.download.js_runtime if cfg else ""
    if custom:
        add(OK, "runtime JS", f"configuré : {custom}")
    elif deno is None:
        add(WARN, "runtime JS (deno)", "absent : YouTube risque de casser. Termux : pkg install deno")
    else:
        major_minor = tuple(int(x) for x in deno.split(".")[:2])
        add(OK if major_minor >= (2, 3) else WARN, "runtime JS (deno)", deno + ("" if major_minor >= (2, 3) else " (< 2.3 requis)"))

    # Bibliothèques Python
    try:
        from musique import ytdlp

        age = ytdlp.version_age_days()
        status = OK if age is None or age < 60 else WARN
        add(status, "yt-dlp", ytdlp.version() + (f" ({age} jours)" if age is not None else "")
            + (" → pip install -U yt-dlp yt-dlp-ejs" if status == WARN else ""))
    except ImportError:
        add(FAIL, "yt-dlp", "non installé")
    if importlib.util.find_spec("yt_dlp_ejs") is not None:
        add(OK, "yt-dlp-ejs", "présent")
    else:
        add(WARN, "yt-dlp-ejs", "absent : pip install yt-dlp-ejs")
    for mod in ("ytmusicapi", "mutagen", "requests"):
        try:
            m = __import__(mod)
            add(OK, mod, getattr(m, "__version__", getattr(m, "version_string", "")) or "présent")
        except ImportError:
            add(FAIL, mod, "non installé")

    # Configuration et bibliothèque
    if cfg is None:
        add(FAIL, "configuration", "absente ou invalide (voir config.example.toml)")
    else:
        add(OK, "configuration", str(cfg.config_path or "(variables d'environnement)"))
        lib = cfg.library
        if not lib.is_dir():
            add(FAIL, "bibliothèque", f"{lib} n'existe pas")
        else:
            try:
                from musique.library import STIGNORE_LINE, ensure_work_area

                work = ensure_work_area(lib)
                a, b = work / "tmp" / f"t{uuid.uuid4().hex[:6]}", lib / f".musique-test-{uuid.uuid4().hex[:6]}"
                a.write_bytes(b"x")
                os.replace(a, b)  # même test que pour un vrai fichier
                b.unlink()
                add(OK, "bibliothèque", f"{lib} (écriture + renommage atomique OK)")
                st = lib / ".stignore"
                ok = st.exists() and STIGNORE_LINE in st.read_text(encoding="utf-8")
                add(OK if ok else WARN, ".stignore", "exclut .musique/" if ok else "ligne manquante")
                free = shutil.disk_usage(lib).free / 1e9
                add(OK if free > 1 else WARN, "espace libre", f"{free:.1f} Go (≈ {free * 1000 / 5:.0f} titres Opus)")
            except OSError as e:
                hint = " Termux : lance termux-setup-storage" if is_termux() else ""
                add(FAIL, "bibliothèque", f"écriture impossible : {e}.{hint}")
        try:
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            add(OK, "dossier d'état", str(cfg.state_dir))
        except OSError as e:
            add(FAIL, "dossier d'état", str(e))

    # Termux
    if is_termux():
        add(OK if shutil.which("termux-wake-lock") else WARN, "termux-wake-lock",
            "présent" if shutil.which("termux-wake-lock") else "absent")

    # Réseau
    try:
        import requests

        r = requests.head("https://music.youtube.com", timeout=10)
        add(OK if r.status_code < 500 else WARN, "réseau", f"music.youtube.com → HTTP {r.status_code}")
    except Exception as e:
        add(FAIL, "réseau", f"music.youtube.com injoignable : {e}")

    width = max(len(w) for _, w, _ in results)
    for status, what, detail in results:
        print(f"[{status}] {what:<{width}}  {detail}")
    fails = sum(1 for s, _, _ in results if s == FAIL)
    warns = sum(1 for s, _, _ in results if s == WARN)
    print(f"\n{fails} problème(s) bloquant(s), {warns} avertissement(s).")
    return 1 if fails else 0
