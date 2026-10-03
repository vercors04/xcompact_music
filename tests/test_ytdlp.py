import datetime as dt

import pytest

from musique import ytdlp
from musique.ytdlp import ErrorKind, build_argv, classify


@pytest.mark.parametrize(
    "msg, kind",
    [
        ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] x: Sign in to confirm your age", ErrorKind.UNAVAILABLE),
        ("ERROR: [youtube] x: Sign in to confirm you’re not a bot", ErrorKind.BOT_CHECK),
        ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", ErrorKind.BOT_CHECK),
        ("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>",
         ErrorKind.NETWORK),
        ("ERROR: [download] Got error: The read operation timed out", ErrorKind.NETWORK),
        ("ERROR: [youtube] x: Requested format is not available", ErrorKind.BROKEN),
        ("WARNING: [youtube] x: nsig extraction failed: Some formats may be missing", ErrorKind.BROKEN),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", ErrorKind.BROKEN),
        ("ERROR: something completely different", ErrorKind.OTHER),
    ],
)
def test_classify(msg, kind):
    assert classify(msg) is kind


def test_version_age(monkeypatch):
    monkeypatch.setattr(ytdlp, "version", lambda: "2026.08.19")
    assert ytdlp.version_age_days(dt.date(2026, 10, 2)) == 44
    monkeypatch.setattr(ytdlp, "version", lambda: "2026.08.19.232908")  # version nightly
    assert ytdlp.version_age_days(dt.date(2026, 8, 20)) == 1


def test_argv_never_reencodes(tmp_path):
    argv = build_argv(tmp_path)
    i = argv.index("--audio-format")
    assert argv[i + 1] == "best"  # « best » = remux dans le codec d'origine
    assert "--audio-quality" not in argv
    assert "drc" in argv[argv.index("-f") + 1]  # variantes DRC exclues
    assert "--abort-on-error" in argv  # sinon les erreurs sont avalées en silence


def test_argv_is_understood_by_ytdlp(tmp_path):
    yt_dlp = pytest.importorskip("yt_dlp")
    opts = yt_dlp.parse_options(build_argv(tmp_path, js_runtime="deno")).ydl_opts
    assert opts["format"].startswith("bestaudio[acodec=opus]")
    assert opts["postprocessors"][0] == {"key": "FFmpegExtractAudio", "preferredcodec": "best",
                                         "preferredquality": "5", "nopostoverwrites": False}
    assert "deno" in opts["js_runtimes"]
    assert opts["ignoreerrors"] is False
