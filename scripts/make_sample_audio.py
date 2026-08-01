"""Generate the sample audio clips Kindred plays while there is no real TTS provider.

Recall's audio-output endpoints take **mp3 as base64**, so everything here ends up as
mp3 regardless of how it was produced. Two producers, tried in order:

1. **Windows SAPI** (`System.Speech`) — actual spoken lines, which makes the demo read as
   "the bot talks" rather than "the bot beeps". Free, offline, no API key.
2. **ffmpeg sine tones** — fallback for non-Windows machines so a teammate can still
   generate assets.

`silence.mp3` is not decorative: the Output Audio endpoint refuses to run unless the bot
was created with `automatic_audio_output`, so every bot ships with a 0.3s silent clip in
that slot purely to unlock the endpoint.

Usage:
    python scripts/make_sample_audio.py            # regenerate everything
    python scripts/make_sample_audio.py --force    # overwrite existing files

Output lands in `backend/app/assets/audio/` and is committed, so nobody else needs
ffmpeg installed to run the demo.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "backend" / "app" / "assets" / "audio"
MANIFEST = ASSET_DIR / "manifest.json"

# mp3 encode settings. Mono at 44.1 kHz — meeting audio is voice, stereo buys nothing,
# and a smaller payload means a smaller base64 body on every output_audio call.
MP3_ARGS = ["-ac", "1", "-ar", "44100", "-b:a", "96k", "-codec:a", "libmp3lame"]

# The clips Kindred can play. `id` is what the API and logs refer to; `text` is what the
# clip actually says, so the frontend timeline can show it without transcribing audio.
CLIPS: list[dict[str, str]] = [
    {
        "id": "greeting",
        "text": (
            "Kindred is here and listening. "
            "I'll flag anything that conflicts with your documents."
        ),
    },
    {
        "id": "flag_revenue",
        "text": "Quick flag. That revenue number conflicts with the Q3 board deck, page fourteen.",
    },
    {
        "id": "checking",
        "text": "Good question. Give me a second while I check the documents.",
    },
    {
        "id": "no_context",
        "text": "I don't have anything on that in the context you've given me.",
    },
    {
        "id": "confirms",
        "text": "That one checks out. It matches the board deck.",
    },
    {
        "id": "clarify",
        "text": "Do you mean net revenue or gross revenue? Those tell different stories here.",
    },
]


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")


def _sapi_available() -> bool:
    return sys.platform == "win32" and _have("powershell")


def _sapi_to_wav(text: str, dest: Path) -> None:
    """Render `text` to a wav with the Windows built-in speech synthesizer."""
    # Single-quoted PowerShell strings only need doubled single quotes escaped.
    escaped = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 0; "
        f"$s.SetOutputToWaveFile('{dest}'); "
        f"$s.Speak('{escaped}'); "
        "$s.Dispose()"
    )
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def _tone_to_mp3(dest: Path, freq_hz: int, seconds: float) -> None:
    """Fallback 'voice': a fading sine tone, distinct enough to tell clips apart."""
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={freq_hz}:duration={seconds}:sample_rate=44100",
            "-af", f"afade=t=out:st={max(seconds - 0.15, 0):.2f}:d=0.15,volume=0.5",
            *MP3_ARGS, str(dest),
        ]
    )


def _wav_to_mp3(src: Path, dest: Path) -> None:
    _run(["ffmpeg", "-y", "-i", str(src), *MP3_ARGS, str(dest)])


def _make_silence(dest: Path) -> None:
    """0.3s of silence — the placeholder that unlocks the Output Audio endpoint."""
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
            "-t", "0.3", *MP3_ARGS, str(dest),
        ]
    )


def _make_chime(dest: Path) -> None:
    """Two-note ack chime. Played when Kindred wakes, before it has anything to say."""
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=0.16:sample_rate=44100",
            "-f", "lavfi", "-i", "sine=frequency=1320:duration=0.22:sample_rate=44100",
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[c];[c]afade=t=out:st=0.28:d=0.10,volume=0.35[out]",
            "-map", "[out]", *MP3_ARGS, str(dest),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Overwrite existing clips.")
    args = parser.parse_args()

    if not _have("ffmpeg"):
        print("ffmpeg not found on PATH — it is required to produce mp3.", file=sys.stderr)
        print("  winget install Gyan.FFmpeg   |   brew install ffmpeg", file=sys.stderr)
        return 1

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    use_sapi = _sapi_available()
    print(f"voice source: {'Windows SAPI (spoken)' if use_sapi else 'ffmpeg tones (fallback)'}")

    entries: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for index, clip in enumerate(CLIPS):
            dest = ASSET_DIR / f"{clip['id']}.mp3"
            if dest.exists() and not args.force:
                print(f"  skip   {dest.name} (exists)")
            elif use_sapi:
                wav = tmpdir / f"{clip['id']}.wav"
                _sapi_to_wav(clip["text"], wav)
                _wav_to_mp3(wav, dest)
                print(f"  spoke  {dest.name}")
            else:
                # Spread tones across a musical-ish range so clips are distinguishable.
                _tone_to_mp3(dest, freq_hz=330 + index * 110, seconds=1.6)
                print(f"  tone   {dest.name}")
            entries.append({"id": clip["id"], "file": dest.name, "text": clip["text"]})

    for name, builder in (("silence", _make_silence), ("chime", _make_chime)):
        dest = ASSET_DIR / f"{name}.mp3"
        if dest.exists() and not args.force:
            print(f"  skip   {dest.name} (exists)")
        else:
            builder(dest)
            print(f"  built  {dest.name}")

    MANIFEST.write_text(
        json.dumps({"clips": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {MANIFEST.relative_to(REPO_ROOT)} ({len(entries)} clips)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
