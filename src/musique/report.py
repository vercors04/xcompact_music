"""Affichage des résultats et rapport TSV de fin de lot."""

from __future__ import annotations

import csv
import time
from collections import Counter
from pathlib import Path

from musique.matching import explain
from musique.models import Outcome, Result

MARK = {
    Outcome.DOWNLOADED: "OK ",
    Outcome.PRESENT: "== ",
    Outcome.PLANNED: "-> ",
    Outcome.DOUBTFUL: "?? ",
    Outcome.NOT_FOUND: "-- ",
    Outcome.ERROR: "!! ",
}


def line(i: int, n: int, r: Result, library: Path, verbose: bool = False) -> str:
    head = f"[{i:>{len(str(n))}}/{n}] {MARK[r.outcome]}{r.query.raw}"
    out = [head]
    if r.best is not None and r.outcome in (Outcome.PLANNED, Outcome.DOUBTFUL, Outcome.NOT_FOUND, Outcome.DOWNLOADED):
        out.append(f"      meilleur : {r.best.candidate.label()}  {explain(r.best)}")
    if r.path is not None:
        try:
            shown = r.path.relative_to(library).as_posix()
        except ValueError:
            shown = str(r.path)
        out.append(f"      fichier  : {shown}")
    if r.message:
        out.append(f"      {r.outcome.value:9}: {r.message}")
    if verbose:
        for a in r.alternatives:
            out.append(f"      autre    : {a.candidate.label()}  {explain(a)}")
    return "\n".join(out)


def summary(results: list[Result]) -> str:
    c = Counter(r.outcome for r in results)
    order = [Outcome.DOWNLOADED, Outcome.PRESENT, Outcome.PLANNED, Outcome.DOUBTFUL, Outcome.NOT_FOUND, Outcome.ERROR]
    parts = [f"{c[o]} {o.value}" for o in order if c[o]]
    return "Résumé : " + (" · ".join(parts) if parts else "rien à faire")


KEEP_REPORTS = 100  # un rapport par lot : sans limite, des milliers de fichiers en quelques années


def write_tsv(results: list[Result], reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / time.strftime("%Y%m%d-%H%M%S.tsv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["résultat", "requête", "score", "candidat", "source", "fichier", "message"])
        for r in results:
            b = r.best
            w.writerow([
                r.outcome.value, r.query.raw,
                f"{b.score:.3f}" if b else "", b.candidate.label() if b else "",
                b.candidate.source_id if b else "", str(r.path or ""), r.message,
            ])
    # Les noms sont des dates AAAAMMJJ-HHMMSS : l'ordre alphabétique est l'ordre chronologique.
    for old in sorted(reports_dir.glob("*.tsv"))[:-KEEP_REPORTS]:
        old.unlink(missing_ok=True)
    return path
