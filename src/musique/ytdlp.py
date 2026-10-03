"""Enveloppe autour de yt-dlp : téléchargement sans réencodage + classification des erreurs.

On pilote yt-dlp par ses *options de ligne de commande* (converties par
yt_dlp.parse_options) plutôt que par ses noms d'options internes : les options CLI
sont documentées et stables, les clés internes moins.

Pourquoi classer les erreurs ? Parce que la bonne réaction diffère :
  NETWORK     → réessayer après une pause (coupure Wi-Fi, timeout) ;
  UNAVAILABLE → ce titre précis est inaccessible : essayer le candidat suivant ;
  BOT_CHECK   → YouTube bloque l'IP/le client : inutile d'insister sur ce lot ;
  BROKEN      → YouTube a changé quelque chose et yt-dlp ne suit plus : il faut
                mettre yt-dlp à jour. Inutile de marteler les requêtes suivantes.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

# Opus d'abord (format 251, ~130-165 kbps), en excluant les variantes « DRC » (dynamique
# compressée par YouTube : plus fortes mais écrasées). Repli : meilleur audio quelconque.
FORMAT = "bestaudio[acodec=opus][format_id!*=drc]/bestaudio[format_id!*=drc]/bestaudio"


class ErrorKind(Enum):
    NETWORK = "réseau"
    UNAVAILABLE = "indisponible"
    BOT_CHECK = "blocage anti-bot"
    BROKEN = "extracteur cassé"
    OTHER = "autre"


class SourceError(RuntimeError):
    def __init__(self, kind: ErrorKind, message: str):
        super().__init__(message)
        self.kind = kind


# L'ordre compte : la première famille qui correspond l'emporte.
_RULES: list[tuple[ErrorKind, re.Pattern]] = [
    (ErrorKind.UNAVAILABLE, re.compile(
        r"video unavailable|private video|has been removed|not available in your country|"
        r"this video is not available|members[- ]only|confirm your age|age[- ]restricted|"
        r"copyright|terminated", re.I)),
    (ErrorKind.BOT_CHECK, re.compile(r"sign in to confirm|not a bot|http error 429|too many requests", re.I)),
    (ErrorKind.NETWORK, re.compile(
        r"timed? ?out|connection (?:reset|refused|aborted)|temporary failure in name resolution|"
        r"name or service not known|network is unreachable|getaddrinfo|failed to resolve|"
        r"remote end closed|incompleteread|http error 5\d\d|errno 10[14]|max retries exceeded|"
        r"ssl", re.I)),
    (ErrorKind.BROKEN, re.compile(
        r"nsig|n challenge|signature|unable to extract|requested format is not available|"
        r"http error 403|no video formats|jsc|player response|failed to parse|js runtime", re.I)),
]


def classify(message: str) -> ErrorKind:
    """Famille d'une erreur yt-dlp d'après son message.

    >>> classify("ERROR: [youtube] abc: Video unavailable")
    <ErrorKind.UNAVAILABLE: 'indisponible'>
    >>> classify("ERROR: [youtube] abc: Sign in to confirm you're not a bot")
    <ErrorKind.BOT_CHECK: 'blocage anti-bot'>
    """
    for kind, rx in _RULES:
        if rx.search(message):
            return kind
    return ErrorKind.OTHER


def version() -> str:
    import yt_dlp.version

    return yt_dlp.version.__version__


def version_age_days(today: dt.date | None = None) -> int | None:
    """Âge de la version de yt-dlp (ses versions sont des dates : 2026.08.19)."""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version())
    if not m:
        return None
    released = dt.date(int(m[1]), int(m[2]), int(m[3]))
    return ((today or dt.date.today()) - released).days


def update_hint() -> str:
    age = version_age_days()
    age_txt = f", publiée il y a {age} jours" if age is not None else ""
    return f"yt-dlp {version()}{age_txt}. Mets-le à jour : pip install -U yt-dlp yt-dlp-ejs"


class _Logger:
    """Redirige les messages de yt-dlp vers `logging` et garde la dernière erreur."""

    def __init__(self) -> None:
        self.last_error = ""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        self.last_error = msg
        log.debug("yt-dlp: %s", msg)


def build_argv(dest_dir: Path, retries: int = 3, js_runtime: str = "", cookies_file: str = "",
               ffmpeg_location: str = "") -> list[str]:
    argv = [
        "-f", FORMAT,
        # -x + « best » : on extrait la piste audio dans son codec d'origine (remux
        # webm → .opus, mp4 → .m4a). Aucun réencodage.
        "-x", "--audio-format", "best",
        "--no-playlist",
        # Par défaut la CLI de yt-dlp *ignore* les erreurs de téléchargement (renvoie
        # des infos sans fichier) : on veut une exception, pour la classer et réagir.
        "--abort-on-error",
        "--no-mtime",  # date du fichier = maintenant (pas la date de mise en ligne)
        "-R", str(retries), "--fragment-retries", str(retries),
        "-o", str(dest_dir / "audio.%(ext)s"),
        "--no-progress", "--quiet",
    ]
    if js_runtime:
        argv += ["--js-runtimes", js_runtime]
    if cookies_file:
        argv += ["--cookies", cookies_file]
    if ffmpeg_location:
        argv += ["--ffmpeg-location", ffmpeg_location]
    return argv


def download_audio(url: str, dest_dir: Path, *, retries: int = 3, js_runtime: str = "",
                   cookies_file: str = "", ffmpeg_location: str = "") -> tuple[Path, dict]:
    """Télécharge l'audio de `url` dans `dest_dir`. Renvoie (chemin, infos du format).

    Les erreurs réseau sont réessayées (pause 5 s, 15 s) ; les autres remontent
    sous forme de SourceError classée.
    """
    import yt_dlp

    argv = build_argv(dest_dir, retries, js_runtime, cookies_file, ffmpeg_location)
    opts = yt_dlp.parse_options(argv).ydl_opts
    attempt = 0
    while True:
        logger = _Logger()
        opts["logger"] = logger
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            downloads = info.get("requested_downloads") or []
            if downloads and downloads[0].get("filepath") and Path(downloads[0]["filepath"]).exists():
                fmt = {k: info.get(k) for k in ("format_id", "acodec", "abr", "asr", "ext", "duration")}
                return Path(downloads[0]["filepath"]), fmt
            msg, cause = logger.last_error or "yt-dlp n'a produit aucun fichier", None
        except yt_dlp.utils.DownloadError as e:
            msg, cause = str(e) or logger.last_error, e
        kind = classify(msg)
        delay = retry_delay(kind, msg, attempt)
        if delay is None:
            raise SourceError(kind, _short(msg)) from cause
        log.info("%s, nouvel essai dans %d s : %s", kind.value, delay, _short(msg))
        time.sleep(delay)
        attempt += 1


def retry_delay(kind: ErrorKind, msg: str, attempt: int) -> int | None:
    """Pause avant un nouvel essai, ou None s'il ne faut pas réessayer.

    * réseau : 2 nouveaux essais (5 s puis 15 s) ;
    * HTTP 403 : 1 nouvel essai. Mesuré sur le téléphone : un 403 isolé sur le flux
      (« unable to download video data ») disparaît à l'essai suivant, car une
      nouvelle extraction fournit une nouvelle URL. Un 403 qui persiste signale en
      revanche un yt-dlp dépassé (→ BROKEN, coupe-circuit).

    >>> retry_delay(ErrorKind.NETWORK, "timed out", 0), retry_delay(ErrorKind.NETWORK, "timed out", 2)
    (5, None)
    >>> retry_delay(ErrorKind.BROKEN, "HTTP Error 403: Forbidden", 0), retry_delay(ErrorKind.BROKEN, "HTTP Error 403", 1)
    (3, None)
    """
    if kind is ErrorKind.NETWORK and attempt < 2:
        return (5, 15)[attempt]
    if kind is ErrorKind.BROKEN and "403" in msg and attempt == 0:
        return 3
    return None


def _short(msg: str) -> str:
    msg = msg.replace("ERROR: ", "").strip()
    return msg.splitlines()[0][:300] if msg else "erreur inconnue"
