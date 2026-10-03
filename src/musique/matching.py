"""Score de confiance entre une requête et un candidat, et décision.

Le score est un produit de facteurs dans [0, 1] :

    score = titre^0.55 × artiste^0.45 × variantes × durée × rang

* titre, artiste : similarités (voir textnorm). On combine par moyenne géométrique
  pondérée plutôt qu'arithmétique : avec une moyenne arithmétique, une reprise par un
  autre artiste (titre 1.0, artiste 0.2) obtiendrait 0.64, presque acceptable ; en
  géométrique elle obtient 1.0^0.55 × 0.2^0.45 = 0.48, nettement rejetée. Un facteur
  faible suffit à faire chuter le score, c'est le comportement voulu.
* variantes : × 0.65 pour chaque variante non demandée (live, remix...) ou demandée
  mais absente. Un seul écart fait passer un match parfait (1.0) à 0.65, sous le
  seuil d'acceptation : on ne télécharge jamais un live à la place du studio sans
  demander. Exception, les variantes « douces » (edit, clean) : c'est le même
  enregistrement raccourci/censuré, × 0.88 seulement. Cas réel : sur YouTube Music,
  toutes les versions officielles d'« Alors on danse » (Stromae) s'appellent
  « Radio Edit », y compris celle de l'album. Quand la version longue existe, elle
  gagne de toute façon (1.0 contre 0.88).
* durée : seulement si la durée attendue est connue (playlist). ±3 s → 1.0, puis
  décroissance linéaire jusqu'à un plancher de 0.3.
* rang : −1 % par position, pour départager deux candidats autrement identiques
  en faveur de l'ordre de pertinence de la source.

Seuils (0.85 / 0.60) choisis sur des cas réels : bons matchs ≥ 0.88, mauvais candidats
plausibles ≤ 0.67 (Radio Edit, Mellow Mix, medley live, reprise).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from musique.models import Candidate, Decision, Query, Scored
from musique.textnorm import (
    artist_similarity,
    featured_artists,
    has_unknown_qualifier,
    unrequested_guests,
    is_live_album,
    similarity,
    split_title,
    variant_counts,
)


@dataclass(frozen=True)
class MatchConfig:
    accept: float = 0.85
    doubtful: float = 0.60
    w_title: float = 0.55
    w_artist: float = 0.45
    variant_penalty: float = 0.35
    soft_variant_penalty: float = 0.12  # pour SOFT_VARIANTS
    duration_tolerance_s: float = 3.0
    duration_scale_s: float = 30.0
    duration_floor: float = 0.3
    rank_penalty: float = 0.01
    tie_margin: float = 0.02


SOFT_VARIANTS = frozenset({"edit", "clean"})


def variant_factor(wanted: Counter[str], got: Counter[str], cfg: MatchConfig) -> float:
    """Produit des pénalités pour chaque écart de variante entre demande et candidat.

    >>> variant_factor(Counter(), Counter({"live": 1}), MatchConfig())
    0.65
    >>> variant_factor(Counter(), Counter({"edit": 1}), MatchConfig())
    0.88
    """
    f = 1.0
    for tag in set(wanted) | set(got):
        diff = abs(wanted[tag] - got[tag])
        penalty = cfg.soft_variant_penalty if tag in SOFT_VARIANTS else cfg.variant_penalty
        f *= (1.0 - penalty) ** diff
    return round(f, 6)


def duration_factor(expected: float | None, actual: float | None, cfg: MatchConfig) -> float:
    """1.0 si l'écart est dans la tolérance, puis décroissance linéaire jusqu'au plancher.

    >>> duration_factor(430, 431, MatchConfig())
    1.0
    >>> duration_factor(430, 242, MatchConfig())  # radio edit : 188 s d'écart
    0.3
    """
    if not expected or not actual:
        return 1.0
    d = abs(expected - actual)
    if d <= cfg.duration_tolerance_s:
        return 1.0
    return max(cfg.duration_floor, 1.0 - (d - cfg.duration_tolerance_s) / cfg.duration_scale_s)


def score_candidate(q: Query, c: Candidate, cfg: MatchConfig = MatchConfig()) -> Scored:
    cand_core, _ = split_title(c.title)
    details: dict[str, float] = {}

    if q.title is not None and q.artist is not None:
        q_core, _ = split_title(q.title)
        title_sim = max(similarity(q_core, cand_core), similarity(q.title, c.title))
        # Les invités du titre comptent comme artistes du candidat : « Nouvelle Vague,
        # Camille » doit valoir 1,0 face à « Nouvelle Vague - … (feat. Camille) ».
        artist_sim = artist_similarity(q.artist, c.artists + featured_artists(c.title))
        base = (title_sim**cfg.w_title) * (artist_sim**cfg.w_artist)
        details.update(title=title_sim, artist=artist_sim)
        wanted = variant_counts(q.title)
    else:
        # Texte libre : on ne sait pas où est l'artiste ni si les qualificatifs font
        # partie de la demande (« radiohead creep acoustic ») : on essaie les deux ordres,
        # avec le cœur du titre et avec le titre complet. Les variantes restent contrôlées
        # à part, donc accepter le titre complet ne laisse pas passer un live non demandé.
        a = " ".join(c.artists)
        base = max(similarity(q.raw, f"{a} {t}") for t in (cand_core, c.title))
        base = max(base, max(similarity(q.raw, f"{t} {a}") for t in (cand_core, c.title)))
        details.update(text=base)
        wanted = variant_counts(q.raw)

    got = variant_counts(c.title)
    if is_live_album(c.album):
        got["live"] += 1
    if q.album and is_live_album(q.album):
        wanted["live"] += 1
    v = variant_factor(wanted, got, cfg)
    # Artiste invité non demandé (voir _penalise_guests pour la règle complète) :
    # crédité comme artiste → variante douce dès maintenant ; seulement dans le titre
    # (« Angel (feat. Horace Andy) ») → neutre ici.
    if q.artist is not None and q.title is not None:
        if unrequested_guests(q.artist, q.title, c.artists):
            v *= 1.0 - cfg.soft_variant_penalty
            details["guest"] = 1.0
        elif unrequested_guests(q.artist, q.title, featured_artists(c.title)):
            details["guest"] = 0.5
    d = duration_factor(q.duration, c.duration, cfg)
    r = 1.0 - cfg.rank_penalty * min(c.rank, 10)
    details.update(variants=round(v, 6), duration=d, rank=r)
    return Scored(candidate=c, score=base * v * d * r, details=details)


def rank(q: Query, candidates: list[Candidate], cfg: MatchConfig = MatchConfig()) -> list[Scored]:
    """Tous les candidats notés, du meilleur au moins bon.

    Départage des quasi-égalités (écart < tie_margin) : version explicite d'abord
    (la version « clean » est censurée), puis meilleure qualité audio.
    """
    scored = [score_candidate(q, c, cfg) for c in candidates]
    _penalise_guests(scored, cfg)
    scored.sort(key=lambda s: -s.score)
    if not scored:
        return scored
    top = scored[0].score
    head = [s for s in scored if s.score >= top - cfg.tie_margin]
    tail = [s for s in scored if s.score < top - cfg.tie_margin]
    head.sort(
        key=lambda s: (s.candidate.explicit is True, s.candidate.quality.rank_key(), s.score),
        reverse=True,
    )
    return head + tail


def _penalise_guests(scored: list[Scored], cfg: MatchConfig) -> None:
    """Invité non demandé : variante forte *s'il existe une version solo au nom propre*.

    Le même texte « (feat. X) » désigne tantôt l'original, tantôt une autre version ; seul
    le reste des résultats permet de trancher. Cas réels (YouTube Music, 2026-10-03) :
    * « Stromae - Alors on danse » : « Alors On Danse (feat. Kanye West) » (autre
      enregistrement, couplets ajoutés) est classé en tête, crédité à Stromae seul ; mais
      une version solo au nom propre existe (« (Radio Edit) », variante connue) → la
      version avec Kanye passe à ×0,65 et la Radio Edit est choisie ;
    * « Massive Attack - Angel » : l'original est « Angel (feat. Horace Andy) » ; la seule
      autre version, « Angel (Angel Dust) », porte un qualificatif inconnu (c'est un
      remix) → pas une preuve de version solo → l'original garde 1,0 ;
    * « Daft Punk - Get Lucky » : toutes les versions créditent Pharrell → ×0,88, accepté.
    « Version solo au nom propre » = sans invité, titre et artiste ≥ 0,9, au plus une
    variante douce, aucun qualificatif inconnu.
    """
    soft = 1.0 - cfg.soft_variant_penalty
    solo_exists = any(
        "guest" not in s.details and s.details.get("title", 0) >= 0.9 and s.details.get("artist", 0) >= 0.9
        and s.details.get("variants", 1.0) >= soft and not has_unknown_qualifier(s.candidate.title)
        for s in scored
    )
    if not solo_exists:
        return
    for s in scored:
        if "guest" in s.details:
            already = soft if s.details["guest"] == 1.0 else 1.0  # pénalité douce déjà appliquée ?
            factor = (1.0 - cfg.variant_penalty) / already
            s.score *= factor
            s.details["variants"] = round(s.details["variants"] * factor, 6)
            s.details["guest"] = 2.0  # pénalité forte appliquée


def decide(best: Scored | None, cfg: MatchConfig = MatchConfig()) -> Decision:
    if best is None or best.score < cfg.doubtful:
        return Decision.REJECT
    if best.score < cfg.accept:
        return Decision.DOUBTFUL
    return Decision.ACCEPT


_WS = re.compile(r"\s+")


def explain(s: Scored) -> str:
    """Résumé lisible du calcul, pour --dry-run et le rapport."""
    parts = [f"{k}={v:.2f}" for k, v in s.details.items()
             if k != "guest" and not (k in ("duration", "rank", "variants") and v == 1.0)]
    if s.details.get("guest", 0.0) >= 1.0:  # 1 : pénalité douce, 2 : forte ; 0,5 : sans effet
        parts.append("invité non demandé")
    return _WS.sub(" ", f"score={s.score:.2f} ({', '.join(parts)})")
