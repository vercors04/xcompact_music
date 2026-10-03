"""Structures de données partagées entre les modules.

Aucune logique ici : uniquement des conteneurs typés, pour que chaque étape de la
chaîne (recherche → score → téléchargement → tags → gain → rangement) échange des
objets explicites plutôt que des dictionnaires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class Query:
    """Une demande de l'utilisateur.

    `artist`/`title` sont renseignés quand la requête a la forme « artiste - titre ».
    Sinon seul `raw` est utilisable (recherche en texte libre).
    `duration` (secondes) est connue quand la requête vient d'une playlist : c'est
    alors un critère de matching très fort.
    """

    raw: str
    artist: str | None = None
    title: str | None = None
    duration: float | None = None
    album: str | None = None


@dataclass(frozen=True)
class Quality:
    """Qualité estimée d'un flux audio, pour comparer des sources entre elles."""

    codec: str  # "opus", "aac", "mp3", "flac", "vorbis"...
    bitrate_kbps: float | None = None
    lossless: bool = False

    # Débit « équivalent Opus » : un AAC à 128 kbps sonne à peu près comme un Opus
    # à ~100 kbps, un MP3 à 128 comme un Opus à ~80. Ordres de grandeur tirés des
    # tests d'écoute publics (hydrogenaudio) ; ils ne servent qu'à départager.
    _EFFICIENCY = {"opus": 1.0, "vorbis": 0.85, "aac": 0.8, "mp3": 0.6}

    def rank_key(self) -> tuple[int, float]:
        """Clé de tri : sans perte d'abord, puis débit équivalent décroissant."""
        eff = self._EFFICIENCY.get(self.codec, 0.5)
        return (1 if self.lossless else 0, (self.bitrate_kbps or 0.0) * eff)

    def __str__(self) -> str:
        if self.lossless:
            return self.codec
        if self.bitrate_kbps:
            return f"{self.codec} {self.bitrate_kbps:.0f}k"
        return self.codec


@dataclass
class Candidate:
    """Un résultat de recherche d'une source."""

    source: str  # nom du plugin, ex. "ytmusic"
    id: str  # identifiant dans la source (videoId pour YouTube)
    title: str
    artists: list[str]
    album: str | None = None
    album_id: str | None = None
    duration: float | None = None  # secondes
    rank: int = 0  # position dans les résultats de la source (0 = premier)
    quality: Quality = field(default_factory=lambda: Quality("unknown"))
    explicit: bool | None = None
    extra: dict = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        """Identifiant global, unique toutes sources confondues (ex. "ytmusic:9RfVp-GhKfs")."""
        return f"{self.source}:{self.id}"

    def label(self) -> str:
        dur = f"{int(self.duration // 60)}:{int(self.duration % 60):02d}" if self.duration else "?:??"
        album = f" [{self.album}]" if self.album else ""
        return f"{', '.join(self.artists)} - {self.title}{album} ({dur})"


@dataclass
class Scored:
    """Un candidat et son score de confiance (0 → 1), avec le détail du calcul."""

    candidate: Candidate
    score: float
    details: dict[str, float] = field(default_factory=dict)


class Decision(Enum):
    ACCEPT = "accept"  # confiance suffisante : on télécharge
    DOUBTFUL = "doubtful"  # zone grise : on demande (--confirm) ou on met de côté
    REJECT = "reject"  # rien d'assez proche : introuvable


@dataclass
class TrackMeta:
    """Métadonnées finales écrites dans le fichier."""

    title: str
    artists: list[str]
    album: str | None = None
    album_artists: list[str] = field(default_factory=list)
    year: int | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    release_type: str | None = None  # "album", "single", "ep" (convention MusicBrainz)
    cover_url: str | None = None
    source_id: str | None = None
    explicit: bool | None = None
    # Identifiants MusicBrainz : track (enregistrement), release, release_group, artists (liste)
    mbids: dict = field(default_factory=dict)

    @property
    def artist_display(self) -> str:
        return ", ".join(self.artists)

    @property
    def album_artist_display(self) -> str:
        return ", ".join(self.album_artists or self.artists[:1])


@dataclass(frozen=True)
class Loudness:
    """Résultat de mesure EBU R128 d'un fichier."""

    integrated_lufs: float  # loudness intégrée (LUFS)
    lra_lu: float  # loudness range (LU)
    peak_dbfs: float  # pic (échantillon ou true peak selon la mesure), dBFS
    true_peak: bool  # True si `peak_dbfs` est un true peak (suréchantillonné)


@dataclass(frozen=True)
class Gain:
    """Gain ReplayGain décidé pour un fichier (référence -18 LUFS)."""

    gain_db: float  # gain à appliquer à la lecture
    target_gain_db: float  # gain qu'il faudrait pour atteindre exactement la cible
    capped: bool  # True si le gain a été réduit pour éviter l'écrêtage
    peak_linear: float  # pic mesuré, en amplitude linéaire (1.0 = 0 dBFS)


class Outcome(Enum):
    DOWNLOADED = "téléchargé"
    PRESENT = "déjà présent"
    DOUBTFUL = "douteux"
    NOT_FOUND = "introuvable"
    ERROR = "erreur"
    PLANNED = "prévu (dry-run)"


@dataclass
class Result:
    """Bilan du traitement d'une requête."""

    query: Query
    outcome: Outcome
    message: str = ""
    path: Path | None = None
    best: Scored | None = None
    alternatives: list[Scored] = field(default_factory=list)
    # Candidats proposables au choix manuel de fin de lot (douteux / introuvable), du
    # meilleur au moins bon. Vide quand l'utilisateur a déjà répondu « aucun ».
    choices: list[Scored] = field(default_factory=list)
