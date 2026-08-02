"""The reasoning pipeline: what Meet AGI does after every person finishes speaking.

    handle_final_segment(segment)
        │
        ├── kill phrase heard ─▶ stop: cancel audio and reasoning, immediately
        ├── wake word heard ───▶ speech mode: retrieve, answer, speak, type into chat
        └── otherwise ─────────▶ ambient: triage, retrieve, find a contradiction, type

Consumers call `handle_final_segment` once per finalized transcript segment and get on
with their lives — it dispatches and returns. The fixture harness and the Recall
transcript stream both call it.

`handle_stop` is the other entry point, and the only one ingest may call on partial
transcript. It is a cancellation, so it does not queue behind anything.
"""

from .context import ConversationMemory, MeetingContext, Turn
from .engine import PipelineEngine, engine, handle_final_segment, handle_stop
from .gate import GateDecision, InterjectionGate
from .reason import Answer, Verdict, answer_question, check_claim
from .triage import heuristic_is_checkable, is_checkable_claim
from .wake import (
    StopDetector,
    StopMatch,
    WakeDetector,
    WakeMatch,
    build_detector,
    build_stop_detector,
    normalize,
)

__all__ = [
    "Answer",
    "ConversationMemory",
    "GateDecision",
    "InterjectionGate",
    "MeetingContext",
    "PipelineEngine",
    "StopDetector",
    "StopMatch",
    "Turn",
    "Verdict",
    "WakeDetector",
    "WakeMatch",
    "answer_question",
    "build_detector",
    "build_stop_detector",
    "check_claim",
    "engine",
    "handle_final_segment",
    "handle_stop",
    "heuristic_is_checkable",
    "is_checkable_claim",
    "normalize",
]
