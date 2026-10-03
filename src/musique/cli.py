"""Interface en ligne de commande.

    musique get "Daft Punk - Around the World" "Radiohead - Creep"
    musique get -f liste.txt --dry-run
    musique get --playlist "https://music.youtube.com/playlist?list=..."
    musique review          # traiter les requêtes douteuses mises de côté
    musique gain --check    # mesurer l'homogénéité du volume de la bibliothèque
    musique retag           # appliquer MusicBrainz à des fichiers déjà présents
    musique doctor          # vérifier l'installation
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import re
import statistics
import sys
from pathlib import Path

from musique import __version__
from musique.config import Config, ConfigError, load
from musique.device import fix_console_encoding, wake_lock
from musique.matching import explain
from musique.models import Query, Result, Scored

log = logging.getLogger("musique")


# --------------------------------------------------------------------------- #
# Journalisation
# --------------------------------------------------------------------------- #


def _setup_logging(cfg: Config | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("  %(levelname)s %(message)s"))
    root.addHandler(console)
    if cfg is not None:
        logdir = cfg.state_dir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logdir / "musique.log", maxBytes=1_000_000, backupCount=3,
                                                  encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Entrées
# --------------------------------------------------------------------------- #

_LIST_ID = re.compile(r"[?&]list=([\w-]+)")
_VIDEO_SUFFIX = re.compile(r"\s*[\(\[](?:official|officiel|clip|lyrics?|audio|video|vid[ée]o|hd|4k|visuali[sz]er)[^\)\]]*[\)\]]",
                           re.I)


def playlist_queries(url: str) -> list[Query]:
    """Requêtes à partir d'une playlist YouTube / YouTube Music (avec durées)."""
    from ytmusicapi import YTMusic

    m = _LIST_ID.search(url)
    pid = m.group(1) if m else url
    data = YTMusic().get_playlist(pid, limit=None)
    out: list[Query] = []
    for t in data.get("tracks") or []:
        title = t.get("title") or ""
        artists = [a["name"] for a in t.get("artists") or [] if a.get("name")]
        artist = ", ".join(artists)
        if t.get("videoType") != "MUSIC_VIDEO_TYPE_ATV" and " - " in title:
            # Vidéo ordinaire : « Artiste - Titre (Official Video) » ; la chaîne
            # (« ArtisteVEVO ») est un mauvais artiste, le titre de la vidéo est meilleur.
            artist, title = title.split(" - ", 1)
            title = _VIDEO_SUFFIX.sub("", title).strip()
            dur = None  # la durée d'un clip ≠ durée du morceau
        else:
            dur = t.get("duration_seconds")
        if not title:
            continue
        album = (t.get("album") or {}).get("name")
        out.append(Query(raw=f"{artist} - {title}" if artist else title, artist=artist or None,
                         title=title if artist else None, duration=dur, album=album))
    return out


def _gather_queries(args) -> list[Query]:
    from musique.query import collect, read_lines

    lines: list[str] = list(args.queries)
    for f in args.file or []:
        lines += read_lines(f)
    queries = collect(lines)
    for url in args.playlist or []:
        queries += playlist_queries(url)
    return queries


# --------------------------------------------------------------------------- #
# Confirmation interactive
# --------------------------------------------------------------------------- #


def interactive_confirm(q: Query, ranked: list[Scored]) -> Scored | None:
    from musique.pipeline import Skip, StopBatch

    print(f"\n  ?? Requête douteuse : {q.raw}")
    for i, s in enumerate(ranked, 1):
        print(f"     {i}. {s.candidate.label()}   {explain(s)}")
    while True:
        try:
            ans = input(f"     Choix [1-{len(ranked)}], s = plus tard, n = aucun, q = arrêter : ").strip().lower()
        except EOFError:
            raise Skip
        if ans in ("s", ""):
            raise Skip
        if ans == "n":
            return None
        if ans == "q":
            raise StopBatch
        if ans.isdigit() and 1 <= int(ans) <= len(ranked):
            return ranked[int(ans) - 1]


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #


def _run_batch(cfg: Config, queries: list[Query], dry_run: bool, confirm: bool, verbose: bool) -> int:
    from musique.library import LibraryIndex, run_lock
    from musique.pipeline import Context, PendingStore, StopBatch, process
    from musique.report import line, summary, write_tsv
    from musique.sources import build_sources

    if not queries:
        print("Aucune requête. Exemple : musique get \"Daft Punk - Around the World\"")
        return 2
    if not cfg.library.is_dir():
        print(f"Bibliothèque introuvable : {cfg.library}")
        return 2

    results: list[Result] = []
    with run_lock(cfg.state_dir), wake_lock(cfg.wake_lock and not dry_run, cfg.release_wake_lock):
        index = LibraryIndex(cfg.library, cfg.state_dir)
        reread, gone = index.refresh()
        index.save()
        log.debug("index : %d fichiers relus, %d disparus, %d au total", reread, gone, len(index.entries))
        ctx = Context(cfg=cfg, sources=build_sources(cfg), index=index, dry_run=dry_run,
                      confirm=interactive_confirm if confirm else None, pending=PendingStore(cfg.state_dir))
        n = len(queries)
        print(f"{n} requête(s) · bibliothèque : {cfg.library}" + (" · DRY-RUN" if dry_run else ""))
        try:
            for i, q in enumerate(queries, 1):
                r = process(q, ctx)
                results.append(r)
                print(line(i, n, r, cfg.library, verbose), flush=True)
        except (StopBatch, KeyboardInterrupt):
            print("\nInterrompu : les fichiers déjà rangés sont complets, rien n'est à moitié écrit.")

    print()
    print(summary(results))
    for name, reason in ctx.disabled.items():
        print(f"Source {name} désactivée pendant ce lot : {reason}")
    if any(r.outcome.name == "DOUBTFUL" for r in results) and not dry_run:
        print("Douteux mis de côté → musique review")
    if results:
        print(f"Rapport : {write_tsv(results, cfg.state_dir / 'reports')}")
    return 1 if any(r.outcome.name == "ERROR" for r in results) else 0


def cmd_get(cfg: Config, args) -> int:
    return _run_batch(cfg, _gather_queries(args), args.dry_run, args.confirm, args.verbose)


def cmd_review(cfg: Config, args) -> int:
    from musique.pipeline import PendingStore

    queries = PendingStore(cfg.state_dir).queries()
    if not queries:
        print("Aucune requête douteuse en attente.")
        return 0
    return _run_batch(cfg, queries, dry_run=False, confirm=True, verbose=args.verbose)


def cmd_gain(cfg: Config, args) -> int:
    """(Re)calcule les tags de gain, ou vérifie l'homogénéité (--check)."""
    from musique.library import walk_audio, run_lock
    from musique.loudness import compute_gain, measure
    from musique.tagging import read_basic, write_tags

    paths = [Path(p) for p in args.paths] if args.paths else sorted(walk_audio(cfg.library))
    if not paths:
        print("Aucun fichier audio.")
        return 0
    lc = cfg.loudness
    raw_l, eff_l = [], []
    with run_lock(cfg.state_dir), wake_lock(cfg.wake_lock, cfg.release_wake_lock):
        for i, p in enumerate(paths, 1):
            tags = read_basic(p) or {}
            g = tags.get("gain_db")
            name = p.relative_to(cfg.library).as_posix() if p.is_relative_to(cfg.library) else str(p)
            try:
                if args.check:
                    if g is None:
                        print(f"[{i}/{len(paths)}] sans tag de gain : {name}")
                        continue
                    before = measure(p, cfg.ffmpeg)
                    # Vérification réelle : on applique le gain du tag comme le ferait le
                    # lecteur, et on remesure. (Mathématiquement after = before + gain ;
                    # le remesurer prouve que le tag écrit est bien celui qu'on croit.)
                    after = measure(p, cfg.ffmpeg, extra_filter=f"volume={g:.2f}dB")
                    raw_l.append(before.integrated_lufs)
                    eff_l.append(after.integrated_lufs)
                    print(f"[{i}/{len(paths)}] {before.integrated_lufs:6.1f} LUFS  gain {g:+5.1f} dB  →  "
                          f"{after.integrated_lufs:6.1f} LUFS  pic {after.peak_dbfs:+5.1f} dBFS  {name}")
                elif g is None or args.all:
                    loud = measure(p, cfg.ffmpeg, true_peak=lc.true_peak)
                    gain = compute_gain(loud, lc.target_lufs, lc.peak_ceiling_dbfs, lc.cap_positive_gain)
                    write_tags(p, None, None, gain)
                    cap = " (plafonné)" if gain.capped else ""
                    print(f"[{i}/{len(paths)}] {loud.integrated_lufs:6.1f} LUFS → {gain.gain_db:+.1f} dB{cap}  {name}")
            except Exception as e:
                print(f"[{i}/{len(paths)}] !! {name} : {e}")
    if args.check and raw_l:
        def stats(v: list[float]) -> str:
            sd = statistics.pstdev(v) if len(v) > 1 else 0.0
            return f"min {min(v):6.1f}  max {max(v):6.1f}  écart max {max(v) - min(v):5.1f} LU  écart-type {sd:4.1f} LU"
        print(f"\nAvant gain : {stats(raw_l)}")
        print(f"Après gain : {stats(eff_l)}")
        print(f"(cible {lc.target_lufs} LUFS ; les titres plafonnés restent volontairement en dessous)")
    return 0


def cmd_retag(cfg: Config, args) -> int:
    """Applique MusicBrainz à des fichiers existants (tags + pochette ; gain et audio intacts)."""
    import os

    from musique.library import LibraryIndex, run_lock, track_relpath, walk_audio
    from musique.musicbrainz import MBClient, enrich
    from musique.tagging import read_track_meta, write_tags

    paths = [Path(p) for p in args.paths] if args.paths else sorted(walk_audio(cfg.library))
    client = MBClient(contact=cfg.metadata.contact)
    n, done = len(paths), 0
    with run_lock(cfg.state_dir), wake_lock(cfg.wake_lock and not args.dry_run, cfg.release_wake_lock):
        for i, p in enumerate(paths, 1):
            name = p.relative_to(cfg.library).as_posix() if p.is_relative_to(cfg.library) else str(p)
            read = read_track_meta(p)
            if read is None:
                print(f"[{i}/{n}] -- {name} : tags titre/artiste absents, ignoré")
                continue
            old, duration = read
            e = enrich(old, duration, client, cfg.matching, cfg.cover.size)
            if not e.used_musicbrainz:
                print(f"[{i}/{n}] -- {name} : {e.note}")
                continue
            new = e.meta
            changes = [f"{label} : {a!r} → {b!r}" for label, a, b in (
                ("titre", old.title, new.title), ("album", old.album, new.album), ("année", old.year, new.year))
                if a != b]
            target = p.with_name(track_relpath(new, p.suffix, "flat").name) if args.rename else p
            if target != p:
                changes.append(f"nom : {p.name!r} → {target.name!r}")
            print(f"[{i}/{n}] OK {name}")
            print(f"      {new.album} ({new.year}), piste {new.track_number}/{new.track_total}"
                  f"{', pochette MB' if e.cover else ''}")
            for ch in changes:
                print(f"      {ch}")
            if args.dry_run:
                continue
            write_tags(p, new, e.cover, None)  # gain inchangé ; pochette inchangée si MB n'en a pas
            if target != p:
                if target.exists() and not os.path.samefile(target, p):
                    print(f"      !! {target.name} existe déjà : fichier non renommé")
                else:
                    os.replace(p, target)
            done += 1
        if not args.dry_run:
            index = LibraryIndex(cfg.library, cfg.state_dir)
            index.refresh()
            index.save()
    print(f"\n{done} fichier(s) mis à jour" + (" (dry-run : rien n'a été écrit)" if args.dry_run else ""))
    return 0


def cmd_doctor(cfg: Config | None, args) -> int:
    from musique.doctor import run_checks

    return run_checks(cfg, args)


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="fichier de configuration TOML")
    common.add_argument("-v", "--verbose", action="store_true", help="plus de détails (candidats, journal)")

    p = argparse.ArgumentParser(prog="musique", description="Trouve, télécharge, tague et normalise des morceaux.")
    p.add_argument("--version", action="version", version=f"musique {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, **kw) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kw)

    g = add("get", help="télécharger des morceaux")
    g.add_argument("queries", nargs="*", help='requêtes « Artiste - Titre »')
    g.add_argument("-f", "--file", action="append", help="fichier de requêtes (une par ligne, # = commentaire, - = stdin)")
    g.add_argument("-p", "--playlist", action="append", help="URL de playlist YouTube / YouTube Music")
    g.add_argument("-n", "--dry-run", action="store_true", help="montrer ce qui serait téléchargé, sans rien faire")
    g.add_argument("-c", "--confirm", action="store_true", help="demander pour chaque requête douteuse")
    g.set_defaults(func=cmd_get)

    r = add("review", help="traiter les requêtes douteuses mises de côté")
    r.set_defaults(func=cmd_review)

    ga = add("gain", help="calculer les tags de gain manquants, ou vérifier (--check)")
    ga.add_argument("paths", nargs="*", help="fichiers (défaut : toute la bibliothèque)")
    ga.add_argument("--all", action="store_true", help="recalculer même les fichiers déjà tagués")
    ga.add_argument("--check", action="store_true", help="mesurer le volume avant/après gain, sans rien écrire")
    ga.set_defaults(func=cmd_gain)

    rt = add("retag", help="appliquer MusicBrainz à des fichiers existants (gain et audio intacts)")
    rt.add_argument("paths", nargs="*", help="fichiers (défaut : toute la bibliothèque)")
    rt.add_argument("--rename", action="store_true", help="renommer en « Artiste - Titre » (dans le même dossier)")
    rt.add_argument("-n", "--dry-run", action="store_true", help="montrer les changements sans rien écrire")
    rt.set_defaults(func=cmd_retag)

    d = add("doctor", help="vérifier l'installation et la configuration")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    fix_console_encoding()
    args = build_parser().parse_args(argv)
    try:
        cfg = load(args.config)
    except ConfigError as e:
        if args.cmd == "doctor":
            _setup_logging(None, args.verbose)
            return cmd_doctor(None, args)
        print(f"Configuration : {e}", file=sys.stderr)
        return 2
    _setup_logging(cfg, args.verbose)
    return args.func(cfg, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
