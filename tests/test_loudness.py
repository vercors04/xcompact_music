import math

import pytest

from musique.loudness import compute_gain, parse_ebur128_summary, r128_q78
from musique.models import Loudness

# Sortie réelle de ffmpeg 9.0.1 (ebur128=peak=sample:framelog=quiet)
FFMPEG_SAMPLE = """
[Parsed_ebur128_0 @ 000001653a0f6440] Summary:
  Integrated loudness:
    I:         -27.8 LUFS
    Threshold: -37.8 LUFS
  Loudness range:
    LRA:         0.0 LU
    Threshold: -47.8 LUFS
    LRA low:   -27.8 LUFS
    LRA high:  -27.8 LUFS
  Sample peak:
    Peak:      -24.0 dBFS
[out#0/null @ 00000165387b4700] video:0KiB audio:469KiB
"""

FFMPEG_TRUE_PEAK = FFMPEG_SAMPLE.replace("Sample peak:", "True peak:").replace("-24.0 dBFS", "-23.7 dBFS")


def test_parse_sample_peak():
    l = parse_ebur128_summary(FFMPEG_SAMPLE)
    assert (l.integrated_lufs, l.lra_lu, l.peak_dbfs, l.true_peak) == (-27.8, 0.0, -24.0, False)


def test_parse_true_peak():
    l = parse_ebur128_summary(FFMPEG_TRUE_PEAK)
    assert l.true_peak and l.peak_dbfs == -23.7


def test_parse_silence():
    text = FFMPEG_SAMPLE.replace("-27.8 LUFS", "-70.0 LUFS", 1).replace("-24.0 dBFS", "-inf dBFS")
    l = parse_ebur128_summary(text)
    assert l.integrated_lufs == -70.0 and l.peak_dbfs == -math.inf


@pytest.mark.parametrize(
    "lufs, peak, gain, capped",
    [
        (-10.7, 3.6, -7.3, False),   # Daft Punk mesuré sur le téléphone : fort, pic > 0 dBFS
        (-18.0, -0.5, 0.0, False),   # déjà à la cible
        (-22.0, -10.0, 4.0, False),  # calme, beaucoup de marge : gain complet
        (-22.0, -3.0, 2.0, True),    # calme, marge 2 dB jusqu'au plafond -1 dBFS
        (-22.0, -1.0, 0.0, True),    # calme, aucune marge
        (-22.0, 0.5, 0.0, True),     # pic déjà au-dessus du plafond : on ne monte pas, on ne baisse pas non plus
    ],
)
def test_compute_gain(lufs, peak, gain, capped):
    g = compute_gain(Loudness(lufs, 5.0, peak, False))
    assert g.gain_db == pytest.approx(gain)
    assert g.capped is capped
    assert g.target_gain_db == pytest.approx(-18.0 - lufs)


def test_cap_can_be_disabled():
    g = compute_gain(Loudness(-22.0, 5.0, -1.0, False), cap=False)
    assert g.gain_db == pytest.approx(4.0) and not g.capped


def test_peak_linear():
    assert compute_gain(Loudness(-14, 5, 0.0, False)).peak_linear == pytest.approx(1.0)
    assert compute_gain(Loudness(-14, 5, -6.0206, False)).peak_linear == pytest.approx(0.5, rel=1e-4)


@pytest.mark.parametrize("rg, q", [(-7.3, -3149), (0.0, -1280), (5.0, 0), (-12.5, -4480)])
def test_r128_q78(rg, q):
    assert r128_q78(rg) == q
    # Conversion inverse faite par Auxio et GStreamer : q/256 + 5
    assert q / 256 + 5 == pytest.approx(rg, abs=1 / 256)


def test_r128_clamped_to_int16():
    assert r128_q78(500) == 32767
    assert r128_q78(-500) == -32768
