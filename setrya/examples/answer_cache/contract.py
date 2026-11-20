"""Shared public contract for the two-caller synthetic answer application."""
from setrya.store_signals import passage_metadata

from setrya.endpoint_evidence import CHECKPOINT, PRIVATE, PRIVATE_TEXT, PUBLIC, PUBLIC_TEXT, QUESTION

# The selective drill asks for a single nearest policy. A new, better-ranked
# document can change its answer even while the original chunk still exists.
REFUND_QUERY = "What is the current refund ceiling?"
DELIVERY_QUERY = "When is the delivery cutoff?"
DELIVERY_TEXT = "Orders before noon ship today."
NEXT_TEXT = "The new refund ceiling is 240 credits."
NEXT = passage_metadata("new-policy", "B", "0")["setrya_marker"]
DELIVERY = passage_metadata("delivery", "1", "0")["setrya_marker"]
SCOPES = {QUESTION: "refund", REFUND_QUERY: "refund", DELIVERY_QUERY: "delivery"}
