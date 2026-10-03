"""Enregistre de vraies réponses MusicBrainz pour les tests (à relancer à la main si besoin).

    python tests/data/capture_mb.py

Chaque cas rejoue exactement les requêtes que fait musique.musicbrainz.enrich et stocke
les réponses dans mb_cases.json ; test_musicbrainz.py les rejoue sans réseau.
"""

import json
from pathlib import Path

from musique.matching import MatchConfig
from musique.models import TrackMeta
from musique.musicbrainz import MBClient, enrich

CASES = {  # nom : (titre YTM, artistes YTM, album YTM, année YTM, durée YTM)
    "creep": ("Creep", ["Radiohead"], "Creep", 1992, 239),
    "around": ("Around the World", ["Daft Punk"], "Homework", 1996, 430),
    "sportsmen": ("Sports Men (2018 Yoshinori Sunahara Remastering)", ["Haruomi Hosono"], "Neue Tanz", None, 246),
    "queen": ("Bohemian Rhapsody", ["Queen"], "A Night At The Opera", 1975, 355),
    "stromae": ("Alors on danse (Radio Edit)", ["Stromae"], "Alors On Danse", 2010, 208),
    "creep_acoustic": ("Creep (Acoustic)", ["Radiohead"], "Creep EP", 1992, 259),
}


class RecordingClient(MBClient):
    def __init__(self):
        super().__init__()
        self.log = []

    def get(self, path, **params):
        data = super().get(path, **params)
        self.log.append({"path": path, "query": params.get("query"), "response": data})
        return data

    def cover(self, release_group_id, size):
        return None  # pas d'images dans les données de test


def main() -> None:
    out = {}
    for name, (title, artists, album, year, dur) in CASES.items():
        c = RecordingClient()
        e = enrich(TrackMeta(title, artists, album=album, year=year), dur, c, MatchConfig())
        print(f"{name}: {e.note}")
        out[name] = {"input": [title, artists, album, year, dur], "calls": c.log}
    Path(__file__).with_name("mb_cases.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":  # jamais à l'import (pytest --doctest-modules importe ce fichier)
    main()
