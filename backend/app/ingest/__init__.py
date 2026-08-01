"""Meeting event ingestion.

Two sources, one normalized event stream:

- `harness` — fixture replay. No network, no keys, deterministic. Milestone 0.
- `recall`  — real Recall.ai bots. Milestone 5.

Everything downstream consumes the normalized stream and cannot tell which source
produced it. That is the whole point: the frontend and the reasoning pipeline are
developed and demoed against the harness, then the real source is swapped in.
"""
