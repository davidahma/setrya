"""Shared public contract for the two-caller synthetic answer application."""
from setrya.store_signals import passage_metadata

QUESTION = "What is the refund policy?"
PRIVATE_TEXT = "The private refund ceiling is 730 credits."
PUBLIC_TEXT = "Contact support for public policy details."
PRIVATE = passage_metadata("policy", "A", "0")["setrya_marker"]
PUBLIC = passage_metadata("guide", "1", "0")["setrya_marker"]
CHECKPOINT = "after-retrieval"
