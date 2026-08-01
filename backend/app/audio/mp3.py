"""Just enough MPEG audio parsing to know how long a clip is.

Kindred has to know a clip's duration to avoid talking over itself: Recall's
`output_audio` returns as soon as the clip is *accepted*, not when it finishes playing,
so the only way to serialize speech is to hold for the clip's own length.

Reading it out of the file beats carrying durations in a manifest, because the clips
that matter later come back from a TTS API as bare bytes with no metadata at all.

Pure stdlib, no ffprobe: a laptop without ffmpeg still runs the demo.
"""

from __future__ import annotations

# Bitrate tables in kbps, indexed by the header's 4-bit bitrate index.
# Index 0 is "free format" and 15 is invalid; both are None and reject the frame.
_BITRATES_V1_L3 = (None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None)
_BITRATES_V2_L3 = (None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None)

# Keyed by the 2-bit version field: 3=MPEG1, 2=MPEG2, 0=MPEG2.5 (1 is reserved).
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}

_LAYER_III = 1  # the 2-bit layer field's value for Layer III


class InvalidMp3(ValueError):
    """The bytes do not contain a decodable MPEG Layer III frame."""


def _skip_id3(data: bytes) -> int:
    """Return the offset past a leading ID3v2 tag, if there is one.

    ffmpeg writes one by default. Its bytes are not audio and contain 0xFF runs that
    would otherwise be mistaken for frame syncs.
    """
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    # Size is 4 syncsafe bytes: 7 bits each, high bit always zero.
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    offset = 10 + size
    if data[5] & 0x10:  # footer present
        offset += 10
    return min(offset, len(data))


def _frame(data: bytes, index: int) -> tuple[int, int, int] | None:
    """Parse a frame header at `index` → (frame_length, samples, sample_rate)."""
    if index + 4 > len(data):
        return None
    # Byte 3 holds channel mode and emphasis, neither of which affects duration.
    b1, b2 = data[index + 1], data[index + 2]
    if data[index] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != _LAYER_III:  # reserved version, or not Layer III
        return None

    bitrate_table = _BITRATES_V1_L3 if version == 3 else _BITRATES_V2_L3
    bitrate = bitrate_table[(b2 >> 4) & 0x0F]
    sample_rate_index = (b2 >> 2) & 0x03
    if bitrate is None or sample_rate_index == 3:
        return None

    sample_rate = _SAMPLE_RATES[version][sample_rate_index]
    padding = (b2 >> 1) & 0x01

    # MPEG1 Layer III packs 1152 samples per frame; MPEG2/2.5 pack 576. The frame-length
    # coefficient is samples_per_frame / 8 bits-per-byte.
    if version == 3:
        samples, coefficient = 1152, 144
    else:
        samples, coefficient = 576, 72

    length = (coefficient * bitrate * 1000) // sample_rate + padding
    if length <= 4:
        return None
    return length, samples, sample_rate


def mp3_duration_ms(data: bytes) -> int:
    """Total playback duration of an mp3, in milliseconds.

    Walks the frame headers and sums each frame's own duration, so VBR files and files
    whose sample rate changes mid-stream come out right.

    Runs ~50-75 ms long against ffprobe: the LAME/Xing header occupies one otherwise
    empty frame that is counted like any other, and encoder delay and end padding are
    described in that header rather than being absent from the stream. Both errors point
    the same way — the estimate is never short — so Kindred waits a fraction too long
    before its next clip rather than talking over its own tail. That is the direction to
    be wrong in, and it is well inside the padding between utterances anyway.
    """
    index = _skip_id3(data)
    total_ms = 0.0
    frames = 0

    while index + 4 <= len(data):
        parsed = _frame(data, index)
        if parsed is None:
            # Not a valid header here — advance a byte and re-sync. Only happens at tag
            # boundaries and in trailing metadata, so the scan stays effectively linear.
            index += 1
            continue
        length, samples, sample_rate = parsed
        total_ms += samples * 1000 / sample_rate
        frames += 1
        index += length

    if frames == 0:
        raise InvalidMp3("no MPEG Layer III frames found — is this really an mp3?")
    return int(round(total_ms))
