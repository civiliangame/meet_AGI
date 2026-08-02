"""The document corpus Meet AGI reasons over.

Plain `.txt` files in `knowledge/`, chunked on `##` headings, retrieved by a keyword
prefilter and handed to Claude. No embeddings, no vector store, no Postgres.

That is a real choice, not a shortcut left for later. The corpus is a few thousand
tokens; a frontier model reading the top handful of chunks beats cosine similarity over
the same content, and it removes a container, a schema, and an embedding provider from
the critical path. `retrieve()` is the seam — swapping in pgvector means reimplementing
one function, and nothing in `app/pipeline/` changes.

Files map back onto the seeded `Document` records by filename stem, so citations carry
a real `document_id` and the frontend can link them.
"""

from .base import Chunk, KnowledgeBase, get_knowledge_base, reload_knowledge_base

__all__ = ["Chunk", "KnowledgeBase", "get_knowledge_base", "reload_knowledge_base"]
