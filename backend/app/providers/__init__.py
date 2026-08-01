"""Swappable model/service providers.

Every sponsor integration sits behind one of these interfaces so any one can be replaced
or dropped without touching the pipeline: `voice/` today, `persona/`, `triage/`, and
`llm/` as they land.
"""
