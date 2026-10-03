"""Mesure de loudness (EBU R128 via ffmpeg) et calcul du gain ReplayGain.

Rappels :
* LUFS = « Loudness Units relative to Full Scale ». Mesure perceptive (filtre K +
  fenêtrage + seuils) standardisée par ITU-R BS.1770 / EBU R128. 1 LU = 1 dB.
* ReplayGain 2.0 vise -18 LUFS. Un titre mesuré à -10.7 LUFS reçoit un gain de
  -18 - (-10.7) = -7.3 dB. Le gain est *stocké dans les tags* et appliqué par le
  lecteur : le fichier audio n'est jamais modifié (pas de réencodage).
* Opus (RFC 7845) utilise une autre convention : R128_TRACK_GAIN, entier en Q7.8
  (1/256 dB), référence -23 LUFS. Auxio et GStreamer convertissent tous deux par
  « valeur/256 + 5 » vers l'échelle -18 (vérifié dans leur code source). Pour qu'un
  Opus joue au même niveau qu'un MP3 tagué -7.3 dB, on écrit donc
  R128_TRACK_GAIN = round((-7.3 - 5) × 256) = -3149.

Plafond anti-écrêtage : un titre calme (-22 LUFS) avec un pic à -1 dBFS recevrait
+4 dB → pic à +3 dBFS → écrêtage, car Auxio ignore les pics. On limite donc les gains
*positifs* pour que pic + gain ≤ plafond (-1 dBFS par défaut). Les gains négatifs ne
sont jamais limités : baisser le volume ne peut pas écrêter.
"""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from musique.models import Gain, Loudness

RG_REFERENCE_LUFS = -18.0
R128_TO_RG_OFFSET_DB = 5.0  # -18 - (-23)


class MeasureError(RuntimeError):
    pass


_SUMMARY = re.compile(r"Summary:(.*)", re.S)
_I = re.compile(r"I:\s+(-?[\d.]+|-?inf|nan)\s+LUFS")
_LRA = re.compile(r"LRA:\s+(-?[\d.]+|nan)\s+LU")
_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-?inf|nan)\s+dBFS")


def parse_ebur128_summary(stderr: str) -> Loudness:
    """Extrait I, LRA et pic du résumé imprimé par le filtre ebur128 de ffmpeg."""
    m = _SUMMARY.search(stderr)
    if not m:
        raise MeasureError("résumé ebur128 introuvable dans la sortie de ffmpeg")
    text = m.group(1)
    i, lra, peak = _I.search(text), _LRA.search(text), _PEAK.search(text)
    if not i:
        raise MeasureError("loudness intégrée absente du résumé ebur128")

    def num(mm: re.Match | None, default: float) -> float:
        if not mm:
            return default
        v = float(mm.group(1))
        return default if math.isnan(v) else v

    true_peak = "True peak:" in text
    return Loudness(
        integrated_lufs=num(i, -70.0),
        lra_lu=num(lra, 0.0),
        peak_dbfs=num(peak, -math.inf),
        true_peak=true_peak,
    )


def measure(path: Path, ffmpeg: str = "ffmpeg", true_peak: bool = False, extra_filter: str = "") -> Loudness:
    """Mesure un fichier. `extra_filter` (ex. "volume=-7.3dB") sert à vérifier un gain.

    peak=sample coûte ~7 % de temps en plus ; peak=true suréchantillonne ×4 et coûte
    ×7 sur un téléphone (mesuré : 4.5 s contre 32 s pour 7 min d'Opus sur un
    Snapdragon 650). D'où sample par défaut.
    """
    peak = "true" if true_peak else "sample"
    af = f"ebur128=peak={peak}:framelog=quiet"
    if extra_filter:
        af = f"{extra_filter},{af}"
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af", af, "-f", "null", "-"]
    try:
        # Délai maximal : ~4 s par titre sur le téléphone ; 10 min = fichier anormal (ou ffmpeg bloqué).
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as e:
        raise MeasureError("ffmpeg ne répond plus (plus de 10 min)") from e
    except OSError as e:
        raise MeasureError(f"ffmpeg introuvable ou inutilisable : {e}") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-3:])
        raise MeasureError(f"ffmpeg a échoué ({proc.returncode}) : {tail}")
    return parse_ebur128_summary(proc.stderr)


def compute_gain(
    loud: Loudness,
    target_lufs: float = RG_REFERENCE_LUFS,
    ceiling_dbfs: float = -1.0,
    cap: bool = True,
) -> Gain:
    """Gain à appliquer, éventuellement plafonné contre l'écrêtage.

    >>> g = compute_gain(Loudness(-10.7, 4.6, 3.6, False))      # titre fort
    >>> round(g.gain_db, 2), g.capped
    (-7.3, False)
    >>> g = compute_gain(Loudness(-22.0, 10.0, -1.0, False))    # titre calme, pic élevé
    >>> round(g.target_gain_db, 2), round(g.gain_db, 2), g.capped
    (4.0, 0.0, True)
    """
    target_gain = target_lufs - loud.integrated_lufs
    gain = target_gain
    capped = False
    if cap and gain > 0 and math.isfinite(loud.peak_dbfs):
        # Marge disponible avant le plafond ; jamais négative (on ne force pas à baisser).
        headroom = max(0.0, ceiling_dbfs - loud.peak_dbfs)
        if gain > headroom:
            gain, capped = headroom, True
    peak_linear = 10 ** (loud.peak_dbfs / 20) if math.isfinite(loud.peak_dbfs) else 0.0
    return Gain(gain_db=gain, target_gain_db=target_gain, capped=capped, peak_linear=peak_linear)


def r128_q78(gain_db: float) -> int:
    """Gain ReplayGain (référence -18) → valeur R128_TRACK_GAIN (Q7.8, référence -23).

    >>> r128_q78(-7.3)
    -3149
    >>> r128_q78(0.0)  # un gain RG nul = 5 dB sous la référence R128
    -1280
    """
    q = round((gain_db - R128_TO_RG_OFFSET_DB) * 256)
    return max(-32768, min(32767, q))  # entier signé 16 bits
