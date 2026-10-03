# xcompact_music

Command-line tool that turns "Artist - Title" queries into tagged audio files with
consistent loudness. Built for an Android phone running Termux; also runs on Windows and
Linux.

For each query it:

1. searches YouTube Music (official audio tracks only) and scores the results against the
   query: title, artist, duration, and markers such as live, remix or cover. Uncertain
   matches are not downloaded automatically: at the end of the batch they are listed with
   their best candidates, and you type the numbers you want anyway (e.g. `2 15b`);
2. downloads the original Opus stream with yt-dlp, without transcoding;
3. looks up the recording on MusicBrainz for the original album, year, track number and
   cover art;
4. measures loudness with ffmpeg (EBU R128) and writes ReplayGain tags (`R128_TRACK_GAIN`
   for Opus), capped so that a boost never clips. The audio itself is not modified;
5. saves the file as `Artist - Title.opus` in the library folder.

Tracks already in the library are recognised by their tags, even after being moved or
renamed, so running the same list twice downloads nothing new.

## Requirements

- Python 3.10+
- ffmpeg
- deno (yt-dlp needs a JavaScript runtime for YouTube)

Termux: `pkg install python ffmpeg deno`
Windows: `winget install Gyan.FFmpeg DenoLand.Deno`

## Install

```sh
python -m venv .venv
.venv/bin/pip install .        # Windows: .venv\Scripts\pip install .
```

Create the config file, `~/.config/musique/config.toml` (Windows:
`%APPDATA%\musique\config.toml`):

```toml
library = "/storage/XXXX-XXXX/Music"
```

Other options are listed in [config.example.toml](config.example.toml). Then check the
setup:

```sh
musique doctor
```

## Usage

```sh
musique get "Daft Punk - Around the World" "Radiohead - Creep"
musique get -f list.txt              # one query per line
musique get -p PLAYLIST_URL          # YouTube or YouTube Music playlist
musique get -f list.txt --dry-run    # show the choices, download nothing
musique get -f list.txt --confirm    # ask about uncertain matches
                                     # (otherwise: pick them by number at the end of the batch)
musique review                       # go through matches set aside earlier
musique retag                        # refresh tags of existing files from MusicBrainz
musique gain --check                 # loudness before/after gain, per file
musique scan                         # Android: make files show up in music apps (MediaStore)
```

Turn on ReplayGain in your player, in track mode with a 0 dB pre-amp. Tested with Auxio
(Android) and Strawberry (GStreamer).

## Development

```sh
pip install -e ".[test]"
pytest
```

The tests run offline. MusicBrainz responses are replayed from `tests/data/mb_cases.json`.

## Notes

YouTube does not serve lossless audio: the Opus stream (about 130-165 kbps) is the best
quality available without an account. Make sure your use complies with YouTube's terms
and the copyright law where you live.
