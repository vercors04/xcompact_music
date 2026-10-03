"""Métadonnées canoniques depuis MusicBrainz (base ouverte, CC0, sans compte ni clé).

Pourquoi : les métadonnées de YouTube Music sont souvent approximatives. Cas réels :
*Homework* daté 1996 (sorti en 1997), *Creep* rangé dans son single de 1992 plutôt que
dans *Pablo Honey*, *Sports Men* rangé dans une compilation de YMO.

Principe, une fois le morceau choisi sur YouTube Music (on connaît donc son titre, ses
artistes et sa durée à la seconde près) :

1. Recherche d'*enregistrements* (« recordings ») : titre + artiste + fenêtre de durée
   (±10 s). Constat mesuré : pour un titre connu, MusicBrainz renvoie surtout du bruit
   (bootlegs, lives, compilations, tous avec un score de 100) ; « Creep » donne 192
   enregistrements. On ne se fie donc pas à son ordre : chaque enregistrement est noté
   avec notre propre score (matching.py), désambiguïsation comprise (« live, 1993... »).
2. Repli par alias : « Haruomi Hosono » n'est qu'un alias de l'artiste crédité
   « 細野晴臣 », que la recherche d'enregistrements ignore (0 résultat, mesuré). On
   cherche alors l'artiste (la recherche d'artistes connaît les alias), puis ses
   enregistrements par identifiant.
3. Choix de la *sortie* (« release ») parmi toutes celles des enregistrements retenus :
   officielle > promo > bootleg ; sans type secondaire (compilation, live, BO...) ;
   album > EP > single ; la plus ancienne ; CD/numérique de préférence (numéros de
   piste entiers plutôt que « A1 »). L'enregistrement original de Creep apparaît sur
   130 sorties (66 compilations, 35 albums officiels, 10 singles) : la règle donne
   *Pablo Honey* (1993).
4. Pochette : Cover Art Archive, pochette du groupe de sortie choisi.

Tout échec (réseau, serveur occupé, aucune correspondance sûre) laisse les métadonnées
YouTube Music : MusicBrainz améliore, il ne bloque jamais un téléchargement.

Règles d'API respectées : 1 requête/s maximum, User-Agent identifiant l'application.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace

from musique import __version__
from musique.matching import MatchConfig, score_candidate
from musique.models import Candidate, Query, TrackMeta
from musique.textnorm import featured_artists, fold, split_title, variant_counts

log = logging.getLogger(__name__)

API = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"


class MBError(RuntimeError):
    pass


class MBRequestError(MBError):
    """Requête refusée (HTTP 400/404…) : problème de *cette* requête, pas du serveur.
    Ne compte pas pour le coupe-circuit."""


# --------------------------------------------------------------------------- #
# Client HTTP (limité à 1 requête/s, nouveaux essais si le serveur est occupé)
# --------------------------------------------------------------------------- #


class MBClient:
    """Client MusicBrainz : 1 requête/s, nouveaux essais, coupe-circuit.

    Deux pannes observées pendant les tests, traitées différemment :
    * « serveur occupé » (HTTP 503) : fréquent et bref → jusqu'à 3 nouveaux essais
      (2, 5, 10 s) ;
    * coupure réseau du téléphone : chaque échec DNS bloquait ~40 s ; avec 3 nouveaux
      essais on perdait ~3 min par titre → un seul nouvel essai.
    Après `max_failures` échecs d'affilée, MusicBrainz est désactivé pour le reste du
    lot (on garde les métadonnées YouTube Music sans attendre).
    """

    def __init__(self, contact: str = "", min_interval_s: float = 1.05, timeout_s: float = 20.0,
                 max_failures: int = 3):
        import requests

        self.session = requests.Session()
        who = contact or "https://musicbrainz.org/doc/MusicBrainz_API"
        self.session.headers["User-Agent"] = f"musique/{__version__} ( {who} )"
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_failures = max_failures
        self.failures = 0
        self._last = 0.0

    @property
    def disabled(self) -> bool:
        return self.failures >= self.max_failures

    def get(self, path: str, **params) -> dict:
        try:
            data = self._get(path, **params)
        except MBRequestError:
            raise
        except MBError:
            self.failures += 1
            if self.disabled:
                log.warning("MusicBrainz désactivé pour ce lot après %d échecs d'affilée", self.failures)
            raise
        self.failures = 0
        return data

    def _get(self, path: str, **params) -> dict:
        import requests

        if self.disabled:
            raise MBError("désactivé pour ce lot (échecs répétés)")
        params["fmt"] = "json"
        busy_delays, network_delays = [2, 5, 10], [5]
        busy = network = 0
        while True:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(f"{API}/{path}", params=params, timeout=self.timeout_s)
            except requests.RequestException as e:
                if network >= len(network_delays):
                    raise MBError(f"réseau : {type(e).__name__}") from e
                delay, network = network_delays[network], network + 1
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise MBRequestError(f"HTTP {r.status_code}")
                if busy >= len(busy_delays):
                    raise MBError(f"HTTP {r.status_code}")
                delay, busy = busy_delays[busy], busy + 1
            log.debug("MusicBrainz indisponible, nouvel essai dans %d s", delay)
            time.sleep(delay)

    def search_recordings(self, query: str, limit: int = 100) -> list[dict]:
        return self.get("recording", query=query, limit=limit).get("recordings", [])

    def search_artists(self, name: str, limit: int = 5) -> list[dict]:
        return self.get("artist", query=_lucene_escape(name), limit=limit).get("artists", [])

    def cover(self, release_group_id: str, size: int) -> bytes | None:
        """Pochette de face du groupe de sortie (vignettes CAA : 250, 500 ou 1200 px)."""
        import requests

        thumb = min((250, 500, 1200), key=lambda s: abs(s - size))
        try:
            r = self.session.get(f"{CAA}/release-group/{release_group_id}/front-{thumb}", timeout=self.timeout_s)
        except requests.RequestException as e:
            log.debug("Cover Art Archive : %s", e)
            return None
        is_image = r.content[:3] == b"\xff\xd8\xff" or r.content[:8] == b"\x89PNG\r\n\x1a\n"
        if r.status_code != 200 or not is_image:
            return None
        return r.content


_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def _lucene_escape(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


def _phrase(text: str) -> str:
    """Phrase exacte Lucene : « "texte" », guillemets internes échappés."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# Sélection (fonctions pures, testées sur de vraies réponses de l'API)
# --------------------------------------------------------------------------- #


def _credit_names(credit: list[dict]) -> list[str]:
    return [c.get("name") or c.get("artist", {}).get("name", "") for c in credit or []]


def recording_as_candidate(rec: dict) -> Candidate:
    """Un enregistrement MusicBrainz vu comme un candidat, pour réutiliser le score."""
    title = rec.get("title", "")
    if rec.get("disambiguation"):
        title = f"{title} ({rec['disambiguation']})"  # « Creep (live, 1993-07-13: ...) »
    length = rec.get("length")
    return Candidate(
        source="musicbrainz", id=rec["id"], title=title, artists=_credit_names(rec.get("artist-credit")),
        duration=length / 1000 if length else None,
    )


def matching_recordings(recs: list[dict], title: str, artists: list[str], duration: float | None,
                        cfg: MatchConfig, trust_artist: bool = False, margin: float = 0.05) -> list[tuple[float, dict]]:
    """Enregistrements qui correspondent au morceau choisi, avec leur score.

    `trust_artist` : l'artiste a déjà été identifié par son identifiant MusicBrainz
    (repli par alias) ; le nom crédité peut alors être dans une autre écriture
    (« 細野晴臣 » pour « Haruomi Hosono ») sans que ce soit une erreur.
    On garde tous les enregistrements proches du meilleur : MusicBrainz a souvent
    plusieurs fiches pour le même enregistrement (non fusionnées).
    """
    q = Query(raw=f"{artists[0] if artists else ''} - {title}", artist=", ".join(artists) or None,
              title=title, duration=duration)
    scored = []
    for rec in recs:
        c = recording_as_candidate(rec)
        if c.duration is None:
            continue  # sans durée, impossible de distinguer version courte et longue
        if trust_artist:
            c.artists = list(artists)
        s = score_candidate(q, c, cfg).score
        if s >= cfg.accept:
            scored.append((s, rec))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return sorted([(s, r) for s, r in scored if s >= best - margin], key=lambda x: -x[0])


_STATUS_RANK = {"Official": 0, None: 1, "Promotion": 2, "Bootleg": 3}
_PRIMARY_RANK = {"Album": 0, "EP": 1, "Single": 2}
_GOOD_FORMATS = {"CD", "Digital Media", "Enhanced CD", "HDCD", "SACD", "Blu-spec CD", "SHM-CD"}


@dataclass(frozen=True)
class ReleaseChoice:
    recording: dict
    release: dict
    medium: dict
    track: dict

    @property
    def release_group(self) -> dict:
        return self.release.get("release-group") or {}


def release_sort_key(release: dict) -> tuple:
    """Plus petit = meilleur. Voir l'en-tête du module pour la justification."""
    rg = release.get("release-group") or {}
    medium = (release.get("media") or [{}])[0]
    return (
        _STATUS_RANK.get(release.get("status"), 4),
        1 if rg.get("secondary-types") else 0,
        _PRIMARY_RANK.get(rg.get("primary-type"), 3),
        release.get("date") or "9999",
        0 if medium.get("format") in _GOOD_FORMATS else 1,
    )


def choose_release(recordings: list[dict], wanted_title: str | None = None) -> ReleaseChoice | None:
    """Meilleure sortie parmi toutes celles des enregistrements retenus.

    `wanted_title` : on écarte les pistes dont le *titre de piste* porte d'autres
    variantes. Cas réel : une édition américaine de *Pablo Honey* (1993) contient en
    piste 13 « Creep (radio edit) », rattachée au même enregistrement que l'original ;
    sans ce filtre, on rangeait Creep en piste 13/13 au lieu de 2.
    """
    options = []
    for rec in recordings:
        for rel in rec.get("releases") or []:
            medium = (rel.get("media") or [{}])[0]
            track = (medium.get("track") or [{}])[0]
            options.append((release_sort_key(rel), rec, rel, medium, track))
    if wanted_title is not None:
        wanted = variant_counts(wanted_title)
        same = [o for o in options if variant_counts(o[4].get("title") or o[1].get("title", "")) == wanted]
        options = same or options
    options = _around_first_release(options)
    if not options:
        return None
    _, rec, rel, medium, track = min(options, key=lambda o: o[0])
    return ReleaseChoice(rec, rel, medium, track)


ORIGINAL_WINDOW_YEARS = 3


def _around_first_release(options: list[tuple]) -> list[tuple]:
    """Ne garde que les sorties parues dans les 3 ans suivant la première parution
    officielle (hors compilations/lives) du morceau.

    Pourquoi : les types de MusicBrainz sont parfois mal saisis. Cas réel : la
    compilation « 80s Party: Ultimate Eighties Throwback Classics » (2020) y est typée
    « Album » sans « Compilation » ; avec la seule règle « album > single », elle
    l'emportait sur le single original de « Love Will Tear Us Apart » (1980). La date,
    elle, ne ment pas : l'album « d'origine » d'un morceau sort au plus tard quelques
    années après sa première parution (Creep : single 1992, Pablo Honey 1993).
    """
    def year(o) -> int | None:
        return _year(o[2].get("date"))

    def clean_official(o) -> bool:
        rg = o[2].get("release-group") or {}
        return o[2].get("status") == "Official" and not rg.get("secondary-types")

    firsts = [y for o in options if clean_official(o) and (y := year(o)) is not None]
    if not firsts:
        return options
    limit = min(firsts) + ORIGINAL_WINDOW_YEARS
    # Une sortie sans date n'est pas « tardive » : on la garde (ex. Creep EP, non daté
    # dans MusicBrainz, est bien la sortie de « Creep (acoustic) » demandée).
    kept = [o for o in options if (y := year(o)) is None or y <= limit]
    return kept or options


def track_number(track: dict, medium: dict) -> int | None:
    """N° de piste entier. Sur un vinyle, MusicBrainz numérote « A1…B5 » : on utilise
    alors la position dans le support (track-offset, à partir de 0).

    >>> track_number({"number": "7"}, {}), track_number({"number": "B4"}, {"track-offset": 10})
    (7, 11)
    """
    n = _int(track.get("number"))
    if n is not None:
        return n
    offset = medium.get("track-offset")
    return offset + 1 if isinstance(offset, int) else None


def prefer_case(mb: str | None, ytm: str | None) -> str | None:
    """Même texte à la casse près : on évite les CAPITALES d'origine de certains disques.

    >>> prefer_case("SPORTS MEN", "Sports Men"), prefer_case("I Am… Sasha Fierce", "I AM...SASHA FIERCE")
    ('Sports Men', 'I Am… Sasha Fierce')
    """
    if mb and ytm and mb.isupper() and not ytm.isupper() and fold(mb) == fold(ytm):
        return ytm
    return mb or ytm


def _year(date: str | None) -> int | None:
    m = re.match(r"\d{4}", date or "")
    return int(m.group(0)) if m else None


def _int(text) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def apply_choice(meta: TrackMeta, choice: ReleaseChoice) -> TrackMeta:
    """Fusionne la sortie choisie dans les métadonnées YouTube Music.

    Ce qui vient de MusicBrainz : album, année, n° de piste/disque, nombre de pistes,
    type de sortie, identifiants, et le titre *s'il porte les mêmes variantes*.
    Ce qui reste de YouTube Music : les noms d'artistes. Ils correspondent à ce que tu
    as cherché et sont en alphabet latin, alors que MusicBrainz les crédite tels
    qu'imprimés sur le disque (« 細野晴臣 »).
    """
    rec, rel, rg = choice.recording, choice.release, choice.release_group
    title = meta.title
    mb_title = rec.get("title") or title
    # « Sports Men (2018 Yoshinori Sunahara Remastering) » → « Sports Men » : mêmes variantes
    # (aucune), on prend le titre propre. « Alors on danse (Radio Edit) » garde sa
    # mention si MusicBrainz ne l'a pas : sinon un live et un studio porteraient le même nom.
    if variant_counts(mb_title) == variant_counts(meta.title):
        title = prefer_case(mb_title, split_title(meta.title)[0])
    # « Résonances (feat. JP Nataf) » → « Résonances » : l'invité ne doit pas disparaître,
    # il passe dans les artistes (convention de Picard : le titre sans « feat. »).
    artists = list(meta.artists)
    known = {fold(a) for a in artists}
    for guest in featured_artists(meta.title):
        if fold(guest) not in known and f" {fold(guest)} " not in f" {fold(title)} ":
            artists.append(guest)
            known.add(fold(guest))
    rel_credit = _credit_names(rel.get("artist-credit"))
    rec_credit = _credit_names(rec.get("artist-credit"))
    album_artists = artists[:1] if not rel_credit or rel_credit == rec_credit else rel_credit
    disc = _int(choice.medium.get("position"))
    rtype = (rg.get("primary-type") or "").lower() or None
    return replace(
        meta,
        title=title,
        artists=artists,
        album=prefer_case(rel.get("title"), meta.album),
        album_artists=album_artists,
        year=_year(rel.get("date")) or meta.year,
        track_number=track_number(choice.track, choice.medium),
        track_total=choice.medium.get("track-count"),
        disc_number=disc if disc and disc > 1 else None,
        release_type=rtype,
        mbids={
            "track": rec["id"],
            "release": rel["id"],
            "release_group": rg.get("id", ""),
            "artists": [c["artist"]["id"] for c in rec.get("artist-credit") or [] if c.get("artist")],
        },
    )


# --------------------------------------------------------------------------- #
# Enrichissement complet
# --------------------------------------------------------------------------- #


@dataclass
class Enrichment:
    meta: TrackMeta
    cover: bytes | None = None
    used_musicbrainz: bool = False
    note: str = ""
    queries: list[str] = field(default_factory=list)


def enrich(meta: TrackMeta, duration: float | None, client: MBClient, cfg: MatchConfig,
           cover_size: int = 600) -> Enrichment:
    """Complète `meta` avec MusicBrainz. Ne lève jamais : en cas d'échec, renvoie meta inchangé."""
    try:
        return _enrich(meta, duration, client, cfg, cover_size)
    except MBError as e:
        log.info("MusicBrainz indisponible (%s) : métadonnées YouTube Music conservées", e)
        return Enrichment(meta, note=f"MusicBrainz indisponible ({e})")
    except Exception as e:  # réponse inattendue : on ne bloque jamais le téléchargement
        log.warning("MusicBrainz : erreur inattendue (%s: %s)", type(e).__name__, e)
        return Enrichment(meta, note="MusicBrainz : erreur inattendue")


def _enrich(meta: TrackMeta, duration: float | None, client: MBClient, cfg: MatchConfig,
            cover_size: int) -> Enrichment:
    core, _ = split_title(meta.title)
    primary = meta.artists[0] if meta.artists else ""
    dur = ""
    if duration:
        lo, hi = int((duration - 10) * 1000), int((duration + 10) * 1000)
        dur = f" AND dur:[{max(lo, 0)} TO {hi}]"
    out = Enrichment(meta)

    q1 = f"recording:{_phrase(core)} AND artist:{_phrase(primary)}{dur}"
    out.queries.append(q1)
    found = matching_recordings(client.search_recordings(q1), meta.title, meta.artists, duration, cfg)

    if not found:  # repli par alias d'artiste
        artists = [a for a in client.search_artists(primary) if int(a.get("score", 0)) >= 90][:3]
        if artists:
            ids = " OR ".join(a["id"] for a in artists)
            q2 = f"recording:{_phrase(core)} AND arid:({ids}){dur}"
            out.queries.append(q2)
            found = matching_recordings(client.search_recordings(q2), meta.title, meta.artists, duration, cfg,
                                        trust_artist=True)
    if not found:
        out.note = "MusicBrainz : aucune correspondance sûre"
        return out

    choice = choose_release([r for _, r in found], wanted_title=meta.title)
    if choice is None:
        out.note = "MusicBrainz : enregistrement trouvé mais sans sortie"
        return out
    out.meta = apply_choice(meta, choice)
    out.used_musicbrainz = True
    rgid = choice.release_group.get("id")
    if rgid:
        out.cover = client.cover(rgid, cover_size)
    rg = choice.release_group
    kind = rg.get("primary-type") or "?"
    out.note = f"MusicBrainz : {out.meta.album} ({out.meta.year or '?'}, {kind.lower()})"
    return out
