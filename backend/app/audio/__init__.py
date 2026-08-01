"""Audio primitives: a clip, the sinks that play it, and mp3 duration parsing."""

from .clip import AudioClip
from .mp3 import InvalidMp3, mp3_duration_ms
from .sinks import AudioSink, NullAudioSink, RecallAudioSink

__all__ = [
    "AudioClip",
    "AudioSink",
    "InvalidMp3",
    "NullAudioSink",
    "RecallAudioSink",
    "mp3_duration_ms",
]
