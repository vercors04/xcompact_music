"""Interface en ligne de commande.

    musique get "Daft Punk - Around the World" "Radiohead - Creep"
    musique get -f liste.txt --dry-run
    (en fin de lot : choix des douteux / introuvables à télécharger quand même)
    musique get --playlist "https://music.youtube.com/playlist?list=..."
    musique review          # traiter les requêtes douteuses mises de côté
    musique gain --check    # mesurer l'homogénéité du volume de la bibliothèque
    musique retag           # appliquer MusicBrainz à des fichiers déjà présents
    musique scan            # Android : faire apparaître les fichiers dans les applis musique
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
from musique.device import fix_console_encoding, media_scan, wake_lock
from musique.matching import explain
from musique.models import Query, Result, Scored

log = logging.getLogger("musique")


# --------------------------------------------------------------------------- #
# Journalisation
# --------------------------------------------------------------------------- #


class _ConsoleFormatter(logging.Formatter):
    """À l'écran : le message seul, jamais la trace Python (elle reste dans le journal)."""

    def format(self, record: logging.LogRecord) -> str:
        saved = record.exc_info, record.exc_text
        record.exc_info = record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved


def _setup_logging(cfg: Config | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(_ConsoleFormatter("  %(levelname)s %(message)s"))
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
        dur = t.get("duration_seconds")
        if t.get("videoType") != "MUSIC_VIDEO_TYPE_ATV":
            # Vidéo (clip, vidéo d'amateur) : sa durée n'est pas celle du morceau (intro,
            # générique : « Colors (Official Music Video) » 4:25 pour un morceau de 4:07,
            # assez pour une pénalité de durée), et son titre porte « (Official Video) ».
            dur = None
            if " - " in title:
                # « Artiste - Titre (Official Video) » : la chaîne (« ArtisteVEVO ») est un
                # mauvais artiste, le titre de la vidéo est meilleur.
                artist, title = title.split(" - ", 1)
            title = _VIDEO_SUFFIX.sub("", title).strip()
        if not title:
            continue
        album = (t.get("album") or {}).get("name")
        out.append(Query(raw=f"{artist} - {title}" if artist else title, artist=artist or None,
                         title=title if artist else None, duration=dur, album=album))
    return out


class UsageError(Exception):
    """Erreur de l'utilisateur (fichier introuvable…) : message clair, pas de trace."""


def _gather_queries(args) -> list[Query]:
    from musique.query import collect, read_lines

    lines: list[str] = list(args.queries)
    for f in args.file or []:
        try:
            lines += read_lines(f)
        except OSError as e:
            raise UsageError(f"fichier de requêtes illisible : {f} ({e.strerror or e})") from e
    queries = collect(lines)
    for url in args.playlist or []:
        try:
            queries += playlist_queries(url)
        except Exception:  # URL invalide, playlist privée ou supprimée, réseau…
            log.debug("playlist illisible : %s", url, exc_info=True)  # détail complet dans le journal
            print(f"Playlist ignorée (introuvable, privée ou réseau indisponible) : {url}", file=sys.stderr)
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
# Choix manuel en fin de lot
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"(\d+)(?:-(\d+))?([a-z]?)")


def parse_selection(text: str, offered: dict[int, int]) -> list[tuple[int, int]]:
    """« 2 15b 4-6 » → [(2, 0), (15, 1), (4, 0), (5, 0), (6, 0)].

    `offered` : numéro de requête → nombre de candidats proposés. Une lettre choisit le
    candidat (a = le premier, par défaut) ; « 4-6 » = 4, 5 et 6 (les numéros non
    proposés de l'intervalle sont ignorés) ; « tout » = le premier candidat de chaque.
    Lève ValueError avec un message lisible si un élément est invalide.

    >>> parse_selection("2, 15b", {2: 3, 15: 2})
    [(2, 0), (15, 1)]
    >>> parse_selection("1-9", {2: 1, 5: 3})
    [(2, 0), (5, 0)]
    """
    text = text.strip().lower()
    if text in ("tout", "tous", "all", "*"):
        return [(i, 0) for i in sorted(offered)]
    out: list[tuple[int, int]] = []
    for tok in re.split(r"[\s,;]+", text):
        if not tok:
            continue
        m = _TOKEN.fullmatch(tok)
        if not m:
            raise ValueError(f"« {tok} » n'est pas un numéro (ex. 2, 15b, 4-6)")
        lo, hi, letter = int(m[1]), int(m[2] or m[1]), m[3]
        if hi < lo:
            raise ValueError(f"intervalle à l'envers : « {tok} »")
        if letter and hi != lo:
            raise ValueError(f"lettre sur un intervalle : « {tok} » (choisis la lettre numéro par numéro)")
        nums = [n for n in range(lo, hi + 1) if n in offered]
        if not nums:
            raise ValueError(f"{tok} : pas dans la liste proposée")
        k = ord(letter) - ord("a") if letter else 0
        for n in nums:
            if k >= offered[n]:
                raise ValueError(f"{tok} : seulement {offered[n]} candidat(s) pour le n° {n}")
            if (n, k) not in out:
                out.append((n, k))
    return out


def offer_choices(results: list[Result]) -> list[tuple[int, Scored]]:
    """Montre les requêtes non téléchargées et demande lesquelles télécharger quand même.

    Renvoie [(numéro de requête dans le lot, candidat choisi)]. Ne demande rien si
    l'entrée n'est pas un terminal (script, tube) : rien n'est alors choisi.
    """
    from musique.models import Outcome

    offered = {i: r.choices for i, r in enumerate(results, 1)
               if r.outcome in (Outcome.DOUBTFUL, Outcome.NOT_FOUND) and r.choices}
    if not offered:
        return []
    width = len(str(len(results)))
    print(f"\nNon téléchargé{'s' if len(offered) > 1 else ''} : "
          "tu peux choisir de télécharger quand même le candidat trouvé.")
    for i, cands in offered.items():
        r = results[i - 1]
        print(f"  {i:>{width}}. {r.query.raw}   ({r.outcome.value})")
        for k, s in enumerate(cands):
            print(f"  {'':>{width}}   {chr(ord('a') + k)}) {s.candidate.label()}   {explain(s)}")
    if not sys.stdin.isatty():
        print("(entrée non interactive : rien n'est demandé ; relance dans un terminal pour choisir)")
        return []
    counts = {i: len(c) for i, c in offered.items()}
    while True:
        try:
            ans = input("Numéros à télécharger (ex. « 2 15 », « 15b » = 2ᵉ candidat, « tout »), "
                        "Entrée = aucun : ")
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        try:
            picked = parse_selection(ans, counts)
        except ValueError as e:
            print(f"  {e}")
            continue
        return [(i, offered[i][k]) for i, k in picked]


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #


def _run_batch(cfg: Config, queries: list[Query], dry_run: bool, confirm: bool, verbose: bool) -> int:
    from musique.library import LibraryIndex, run_lock
    from musique.pipeline import Context, PendingStore, StopBatch, process, process_chosen
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
            # Choix manuel parmi ce qui n'a pas été téléchargé d'office. Le résultat
            # remplace celui de la requête (résumé et rapport reflètent l'état final).
            if not dry_run:
                picked = offer_choices(results)
                if picked:
                    print()
                replaced: set[int] = set()
                for i, chosen in picked:
                    r = process_chosen(queries[i - 1], chosen, ctx)
                    if i in replaced:  # 2ᵉ candidat choisi pour la même requête : en plus
                        results.append(r)
                    else:
                        results[i - 1] = r
                        replaced.add(i)
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
    from musique.library import rewrite_atomically, run_lock, walk_audio
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
                    rewrite_atomically(p, cfg.library, lambda f: write_tags(f, None, None, gain))
                    media_scan([p], cfg.media_scan)
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

    from musique.library import LibraryIndex, rewrite_atomically, run_lock, track_relpath, walk_audio
    from musique.musicbrainz import MBClient, enrich
    from musique.tagging import has_cover, read_track_meta, same_metadata, write_tags

    paths = [Path(p) for p in args.paths] if args.paths else sorted(walk_audio(cfg.library))
    client = MBClient(contact=cfg.metadata.contact)
    n, done, unchanged = len(paths), 0, 0
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
            if same_metadata(old, new) and target == p and (e.cover is None or has_cover(p)):
                # Rien à changer : on ne réécrit pas (sinon Syncthing renverrait le fichier).
                print(f"[{i}/{n}] == {name} : déjà à jour")
                unchanged += 1
                continue
            if target != p:
                changes.append(f"nom : {p.name!r} → {target.name!r}")
            print(f"[{i}/{n}] OK {name}")
            print(f"      {new.album} ({new.year}), piste {new.track_number}/{new.track_total}"
                  f"{', pochette MB' if e.cover else ''}")
            for ch in changes:
                print(f"      {ch}")
            if args.dry_run:
                continue
            # Gain inchangé ; pochette inchangée si MusicBrainz n'en a pas.
            rewrite_atomically(p, cfg.library, lambda f: write_tags(f, new, e.cover, None))
            scan = [p]
            if target != p:
                if target.exists() and not os.path.samefile(target, p):
                    print(f"      !! {target.name} existe déjà : fichier non renommé")
                else:
                    os.replace(p, target)
                    scan.append(target)  # l'ancien chemin est retiré de l'index Android, le nouveau ajouté
            media_scan(scan, cfg.media_scan)
            done += 1
        if not args.dry_run:
            index = LibraryIndex(cfg.library, cfg.state_dir)
            index.refresh()
            index.save()
    print(f"\n{done} fichier(s) mis à jour, {unchanged} déjà à jour"
          + (" (dry-run : rien n'a été écrit)" if args.dry_run else ""))
    return 0


def cmd_scan(cfg: Config, args) -> int:
    """Signale des fichiers déjà présents à l'index des médias d'Android (Termux)."""
    from musique.device import is_termux
    from musique.library import walk_audio

    if not is_termux():
        print("Rien à faire : l'index des médias n'existe que sous Android (Termux).")
        return 0
    paths = [Path(p) for p in args.paths] if args.paths else sorted(walk_audio(cfg.library))
    if not paths:
        print("Aucun fichier audio.")
        return 0
    print(f"{len(paths)} fichier(s) à signaler à Android (~0,5 s chacun)…")
    n = media_scan(paths)
    print(f"{n}/{len(paths)} signalé(s). Ils apparaissent dans les applis musique d'ici quelques secondes.")
    return 0 if n == len(paths) else 1


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

    sc = add("scan", help="faire apparaître les fichiers dans les applis musique d'Android (Termux)")
    sc.add_argument("paths", nargs="*", help="fichiers (défaut : toute la bibliothèque)")
    sc.set_defaults(func=cmd_scan)

    d = add("doctor", help="vérifier l'installation et la configuration")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    from musique.library import AlreadyRunning

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
    # Codes de sortie : 0 ok, 1 erreur(s) pendant le traitement, 2 erreur d'utilisation,
    # 3 déjà en cours, 130 interrompu (convention Unix pour Ctrl+C).
    try:
        return args.func(cfg, args)
    except UsageError as e:
        print(e, file=sys.stderr)
        return 2
    except AlreadyRunning as e:
        print(f"{e} : attends qu'il se termine.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrompu. Rien n'est à moitié écrit : les fichiers sont modifiés d'un coup ou pas du tout.",
              file=sys.stderr)
        return 130
    except Exception as e:  # bug ou cas imprévu : message court, détails dans le journal
        log.exception("erreur inattendue")
        print(f"Erreur inattendue : {type(e).__name__}: {e}\nDétails : {cfg.state_dir / 'logs' / 'musique.log'}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
