"""Traitement d'une requête de bout en bout, isolé des autres.

    requête ─► déjà connue ? ─► recherche (toutes sources) ─► score ─► décision
                                                                   │
              ┌──────────────── ACCEPT ◄──────────┬────────── DOUBTFUL ─► confirmation / mis de côté
              ▼                                   └────────── REJECT ──► introuvable
    métadonnées ─► téléchargement (.musique/tmp) ─► vérification ffprobe
              ─► mesure loudness ─► gain ─► tags + pochette ─► os.replace ─► index

Une exception dans une requête est convertie en Result(ERROR) : le lot continue.
Les pannes « structurelles » d'une source (yt-dlp cassé, blocage anti-bot) la
désactivent pour le reste du lot (coupe-circuit) au lieu de produire N fois la même
erreur et d'aggraver un éventuel blocage.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from musique import ytdlp
from musique.config import Config
from musique.library import LibraryIndex, place_atomically, resolve_existing, temp_dir, track_relpath
from musique.loudness import compute_gain, measure
from musique.matching import decide, rank
from musique.media import fetch_cover, probe
from musique.models import Decision, Outcome, Query, Result, Scored
from musique.musicbrainz import MBClient, enrich
from musique.query import query_key
from musique.sources.base import Source
from musique.tagging import write_tags
from musique.ytdlp import ErrorKind, SourceError

log = logging.getLogger(__name__)

# Callback de confirmation : reçoit la requête et les meilleurs candidats, renvoie le
# candidat choisi, None (« aucun ne convient ») ou lève StopBatch.
ConfirmFn = Callable[[Query, list[Scored]], Scored | None]


class StopBatch(Exception):
    """L'utilisateur a demandé d'arrêter le lot."""


class Skip(Exception):
    """L'utilisateur a demandé de laisser cette requête de côté (reste douteuse)."""


@dataclass
class Context:
    cfg: Config
    sources: list[Source]
    index: LibraryIndex
    dry_run: bool = False
    confirm: ConfirmFn | None = None
    disabled: dict[str, str] = field(default_factory=dict)  # source → raison
    strikes: dict[str, int] = field(default_factory=dict)
    last_download: float = 0.0
    pending: "PendingStore | None" = None
    _mb: MBClient | None = None

    def source(self, name: str) -> Source:
        return next(s for s in self.sources if s.name == name)

    def mb_client(self) -> MBClient:
        if self._mb is None:  # un seul client par lot : la limite de 1 requête/s est globale
            self._mb = MBClient(contact=self.cfg.metadata.contact)
        return self._mb


# --------------------------------------------------------------------------- #
# Requêtes douteuses mises de côté (pour `musique review`)
# --------------------------------------------------------------------------- #


class PendingStore:
    def __init__(self, state_dir: Path):
        self.file = state_dir / "pending.json"
        try:
            self.items: dict[str, dict] = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = {}

    def add(self, q: Query, ranked: list[Scored]) -> None:
        self.items[query_key(q)] = {
            "raw": q.raw, "artist": q.artist, "title": q.title, "duration": q.duration, "album": q.album,
            "candidates": [{"label": s.candidate.label(), "id": s.candidate.source_id, "score": round(s.score, 3)}
                           for s in ranked[:5]],
            "added": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.save()

    def discard(self, q: Query) -> None:
        if self.items.pop(query_key(q), None) is not None:
            self.save()

    def queries(self) -> list[Query]:
        return [Query(raw=v["raw"], artist=v["artist"], title=v["title"], duration=v["duration"], album=v["album"])
                for v in self.items.values()]

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)


# --------------------------------------------------------------------------- #
# Coupe-circuit
# --------------------------------------------------------------------------- #


def _source_failed(ctx: Context, src_name: str, err: SourceError) -> None:
    if err.kind not in (ErrorKind.BROKEN, ErrorKind.BOT_CHECK):
        return
    ctx.strikes[src_name] = ctx.strikes.get(src_name, 0) + 1
    # Le blocage anti-bot ne se résout pas en insistant : on coupe tout de suite.
    # Une erreur « extracteur » peut être isolée (une vidéo bizarre) : on tolère 2 échecs.
    if err.kind is ErrorKind.BOT_CHECK or ctx.strikes[src_name] >= 2:
        if err.kind is ErrorKind.BOT_CHECK:
            reason = ("YouTube bloque les requêtes (« confirm you're not a bot »). Attends quelques heures, "
                      "change de réseau, ou fournis des cookies (download.cookies_file).")
        else:
            reason = f"la source ne répond plus comme prévu. {ytdlp.update_hint()}"
        ctx.disabled[src_name] = reason
        log.warning("source %s désactivée pour ce lot : %s (dernière erreur : %s)", src_name, reason, err)


def _source_ok(ctx: Context, src_name: str) -> None:
    ctx.strikes[src_name] = 0


# --------------------------------------------------------------------------- #
# Traitement d'une requête
# --------------------------------------------------------------------------- #


def process(q: Query, ctx: Context) -> Result:
    try:
        return _process(q, ctx)
    except (StopBatch, KeyboardInterrupt):
        raise
    except Exception as e:  # filet de sécurité : une requête ne tue jamais le lot
        log.exception("erreur inattendue pour %r", q.raw)
        return Result(q, Outcome.ERROR, f"erreur inattendue : {type(e).__name__}: {e}")


def _process(q: Query, ctx: Context) -> Result:
    cfg, lib = ctx.cfg, ctx.cfg.library
    key = query_key(q)

    rel = ctx.index.by_query(key)
    if rel:
        return Result(q, Outcome.PRESENT, "déjà téléchargé (requête connue)", path=lib / rel)

    # 1. Recherche dans toutes les sources actives
    candidates, errors = [], []
    for src in ctx.sources:
        if src.name in ctx.disabled:
            errors.append(f"{src.name} désactivée : {ctx.disabled[src.name]}")
            continue
        try:
            candidates += src.search(q, cfg.search_limit)
            _source_ok(ctx, src.name)
        except SourceError as e:
            _source_failed(ctx, src.name, e)
            errors.append(f"{src.name} : {e}")
    if not candidates and errors:
        return Result(q, Outcome.ERROR, " ; ".join(errors))

    # 2. Score et décision
    ranked = rank(q, candidates, cfg.matching)
    best = ranked[0] if ranked else None
    decision = decide(best, cfg.matching)
    res_alts = ranked[1:4]

    if decision is Decision.REJECT:
        return Result(q, Outcome.NOT_FOUND, "aucun candidat assez proche", best=best, alternatives=res_alts)

    if decision is Decision.DOUBTFUL:
        if ctx.confirm is None:
            if ctx.pending is not None and not ctx.dry_run:
                ctx.pending.add(q, ranked)
            return Result(q, Outcome.DOUBTFUL, "confiance insuffisante", best=best, alternatives=res_alts)
        try:
            chosen = ctx.confirm(q, ranked[:5])
        except Skip:
            if ctx.pending is not None:
                ctx.pending.add(q, ranked)
            return Result(q, Outcome.DOUBTFUL, "laissé de côté", best=best, alternatives=res_alts)
        if chosen is None:
            if ctx.pending is not None:
                ctx.pending.discard(q)
            return Result(q, Outcome.NOT_FOUND, "aucun candidat retenu (choix manuel)", best=best)
        best = chosen
        ranked = [chosen] + [s for s in ranked if s is not chosen]

    # 3. Déjà dans la bibliothèque (même identifiant de source) ?
    existing = ctx.index.by_source(best.candidate.source_id)
    if existing:
        ctx.index.remember_query(key, existing)
        ctx.index.save()
        if ctx.pending is not None:
            ctx.pending.discard(q)
        return Result(q, Outcome.PRESENT, "déjà présent (même source)", path=lib / existing, best=best)

    if ctx.dry_run:
        return Result(q, Outcome.PLANNED, "", best=best, alternatives=res_alts)

    # 4. Téléchargement : le meilleur, puis les suivants acceptables s'il est indisponible
    fallbacks = [s for s in ranked[1:] if s.score >= cfg.matching.accept][:2]
    last_error = ""
    for s in [best, *fallbacks]:
        src_name = s.candidate.source
        if src_name in ctx.disabled:
            last_error = f"{src_name} désactivée : {ctx.disabled[src_name]}"
            continue
        try:
            path, msg = _acquire(s, q, ctx)
            _source_ok(ctx, src_name)
            if ctx.pending is not None:
                ctx.pending.discard(q)
            outcome = Outcome.DOWNLOADED if msg != "present" else Outcome.PRESENT
            return Result(q, outcome, "déjà présent (même titre)" if msg == "present" else msg, path=path, best=s)
        except SourceError as e:
            last_error = f"{e.kind.value} : {e}"
            _source_failed(ctx, src_name, e)
            if e.kind is ErrorKind.UNAVAILABLE:
                log.info("%s indisponible, candidat suivant", s.candidate.source_id)
                continue
            break
    return Result(q, Outcome.ERROR, last_error or "échec du téléchargement", best=best)


def _acquire(s: Scored, q: Query, ctx: Context) -> tuple[Path, str]:
    """Télécharge, vérifie, mesure, tague et range un candidat. Renvoie (chemin, message)."""
    cfg, lib = ctx.cfg, ctx.cfg.library
    c = s.candidate
    src = ctx.source(c.source)
    meta = src.metadata(c)

    # Métadonnées canoniques (album original, année, n° de piste) ; jamais bloquant.
    mb = None
    if cfg.metadata.musicbrainz:
        mb = enrich(meta, c.duration, ctx.mb_client(), cfg.matching, cfg.cover.size)
        meta = mb.meta
        log.info("%s : %s", q.raw, mb.note)

    # Même morceau déjà présent (autre videoId, ou fichier que tu as renommé/déplacé) ?
    # 1) mêmes tags artiste + titre n'importe où dans la bibliothèque ;
    # 2) même nom de fichier à l'emplacement prévu (extension la plus probable).
    existing = ctx.index.by_artist_title(meta.artist_display, meta.title)
    if existing:
        ctx.index.remember_query(query_key(q), existing)
        ctx.index.save()
        return lib / existing, "present"
    for ext in (".opus", ".m4a"):
        probable = resolve_existing(lib, track_relpath(meta, ext, cfg.layout))
        if probable.exists():
            ctx.index.note_existing(probable, query_key(q))
            ctx.index.save()
            return probable, "present"

    # Politesse : espacer les téléchargements
    wait = cfg.download.sleep_between_s - (time.monotonic() - ctx.last_download)
    if wait > 0:
        time.sleep(wait)

    with temp_dir(lib) as tmp:
        t0 = time.monotonic()
        audio, quality = src.download(c, tmp)
        ctx.last_download = time.monotonic()
        t_dl = ctx.last_download - t0

        info = probe(audio, cfg.ffprobe)
        if c.duration and info.duration and abs(info.duration - c.duration) > 10:
            raise SourceError(ErrorKind.OTHER, f"durée inattendue : {info.duration:.0f} s au lieu de {c.duration:.0f} s")

        t0 = time.monotonic()
        loud = measure(audio, cfg.ffmpeg, true_peak=cfg.loudness.true_peak)
        t_ld = time.monotonic() - t0
        gain = compute_gain(loud, cfg.loudness.target_lufs, cfg.loudness.peak_ceiling_dbfs, cfg.loudness.cap_positive_gain)

        # Pochette de l'album choisi par MusicBrainz (Cover Art Archive), sinon YouTube Music.
        cover = mb.cover if mb and mb.cover else None
        cover_src = "MB" if cover else ""
        if cover is None and meta.cover_url:
            cover = fetch_cover(meta.cover_url)
            cover_src = "YTM" if cover else ""
        write_tags(audio, meta, cover, gain)

        dest = resolve_existing(lib, track_relpath(meta, audio.suffix, cfg.layout))
        if dest.exists():  # cas rare (extension inattendue) : on ne remplace jamais
            ctx.index.note_existing(dest, query_key(q))
            ctx.index.save()
            return dest, "present"
        place_atomically(audio, dest)

    ctx.index.add(dest, c.source_id, meta.artist_display, meta.title, query_key(q))
    ctx.index.save()
    kbps = f"{info.bitrate_kbps:.0f}k" if info.bitrate_kbps else "?"
    cap = " (plafonné)" if gain.capped else ""
    tags = f"{meta.album or '?'} ({meta.year or '?'})" + (" [MB]" if mb and mb.used_musicbrainz else " [YTM]")
    msg = (f"{info.codec} {kbps}, {loud.integrated_lufs:.1f} LUFS → gain {gain.gain_db:+.1f} dB{cap}, "
           f"album {tags}, pochette {cover_src or 'non'} [dl {t_dl:.0f} s, mesure {t_ld:.0f} s]")
    log.info("%s → %s (%s ; qualité annoncée %s)", q.raw, dest, msg, quality)
    return dest, msg
