"""Optional PostgreSQL search and indexing-worker fixture."""

MODULE = "examples.pgvector_search"
MODES = ("fixed", "broken", "opaque")
SCENARIOS = ("refund", "reupload")
REFUND_QUESTION = "How long do I have to return an order?"
DELIVERY_QUESTION = "When can I track my delivery?"
REUPLOAD_QUESTION = "How do I return an order and get my refund?"
