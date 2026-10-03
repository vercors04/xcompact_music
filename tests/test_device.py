"""Index des médias d'Android : commande envoyée, et jamais bloquant."""

import subprocess
from pathlib import Path

from musique import device


def fake_android(monkeypatch, returncode=0, error=None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if error:
            raise error
        return subprocess.CompletedProcess(argv, returncode, "", "refusé" if returncode else "")

    monkeypatch.setattr(device, "is_termux", lambda: True)
    monkeypatch.setattr(device.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(device.subprocess, "run", run)
    return calls


def test_media_scan_sends_encoded_file_uri(monkeypatch):
    calls = fake_android(monkeypatch)
    p = Path("/storage/C1B3-16F0/Music/Bob Dylan - Don’t Think Twice.opus")
    assert device.media_scan([p]) == 1
    argv = calls[0]
    assert argv[:4] == ["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE"]
    uri = argv[argv.index("-d") + 1]
    assert uri.startswith("file://") and uri.endswith("/Bob%20Dylan%20-%20Don%E2%80%99t%20Think%20Twice.opus")


def test_media_scan_never_raises(monkeypatch):
    fake_android(monkeypatch, error=subprocess.TimeoutExpired("am", 30))
    assert device.media_scan([Path("/x.opus")]) == 0
    fake_android(monkeypatch, returncode=1)
    assert device.media_scan([Path("/x.opus"), Path("/y.opus")]) == 0


def test_media_scan_off_android_or_disabled(monkeypatch):
    calls = fake_android(monkeypatch)
    assert device.media_scan([Path("/x.opus")], enabled=False) == 0
    monkeypatch.setattr(device, "is_termux", lambda: False)
    assert device.media_scan([Path("/x.opus")]) == 0
    assert calls == []
