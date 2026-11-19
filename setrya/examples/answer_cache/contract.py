"""Shared public contract for the two-caller synthetic answer application."""
from setrya.store_signals import passage_metadata

QUESTION = "What is the refund policy?"
PRIVATE_TEXT = "The private refund ceiling is 730 credits."
PUBLIC_TEXT = "Contact support for public policy details."
PRIVATE = passage_metadata("policy", "A", "0")["setrya_marker"]
PUBLIC = passage_metadata("guide", "1", "0")["setrya_marker"]
CHECKPOINT = "after-retrieval"

# The selective drill asks for a single nearest policy. A new, better-ranked
# document can change its answer even while the original chunk still exists.
REFUND_QUERY = "What is the current refund ceiling?"
DELIVERY_QUERY = "When is the delivery cutoff?"
DELIVERY_TEXT = "Orders before noon ship today."
NEXT_TEXT = "The new refund ceiling is 240 credits."
NEXT = passage_metadata("new-policy", "B", "0")["setrya_marker"]
DELIVERY = passage_metadata("delivery", "1", "0")["setrya_marker"]
SCOPES = {QUESTION: "refund", REFUND_QUERY: "refund", DELIVERY_QUERY: "delivery"}
