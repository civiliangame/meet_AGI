"""The reasoning pipeline: what Kindred does after every person finishes speaking.

    handle_final_segment(segment)
        │
        ├── wake word heard ──▶ speech mode: retrieve, answer, speak, type into chat
        └── otherwise ────────▶ ambient: triage, retrieve, find conflicts, gate, type

Consumers call `handle_final_segment` once per finalized transcript segment and get on
with their lives — it dispatches and returns. The fixture harness calls it today; the
Recall transcript stream will call the same function.
"""

from .context import ConversationMemory, MeetingContext, Turn
from .engine import PipelineEngine, engine, handle_final_segment
from .gate import GateDecision, InterjectionGate
from .reason import Answer, Verdict, answer_question, check_claim
from .triage import heuristic_is_checkable, is_checkable_claim
from .wake import WakeDetector, WakeMatch, build_detector, normalize

__all__ = [
    "Answer",
    "ConversationMemory",
    "GateDecision",
    "InterjectionGate",
    "MeetingContext",
    "PipelineEngine",
    "Turn",
    "Verdict",
    "WakeDetector",
    "WakeMatch",
    "answer_question",
    "build_detector",
    "check_claim",
    "engine",
    "handle_final_segment",
    "heuristic_is_checkable",
    "is_checkable_claim",
    "normalize",
]
