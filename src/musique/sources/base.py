"""Interface commune des sources.

Une source sait : chercher des candidats pour une requête, donner les métadonnées
d'un candidat, et télécharger son audio *sans réencodage*. Pour ajouter une source
(SoundCloud, Bandcamp, Internet Archive...), il suffit d'écrire une classe qui
respecte ce protocole et de l'enregistrer dans sources/__init__.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from musique.models import Candidate, Quality, Query, TrackMeta


class Source(Protocol):
    name: str

    def search(self, query: Query, limit: int) -> list[Candidate]:
        """Candidats triés par pertinence selon la source (le score est calculé ailleurs)."""
        ...

    def metadata(self, candidate: Candidate) -> TrackMeta:
        """Métadonnées complètes (album, année, n° de piste, URL de pochette)."""
        ...

    def download(self, candidate: Candidate, dest_dir: Path) -> tuple[Path, Quality]:
        """Télécharge dans dest_dir. Renvoie le fichier et la qualité *réellement* obtenue."""
        ...
