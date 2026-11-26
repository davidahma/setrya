"""Stable fixture API for a scoped erasure drill and its recorded observations."""
from . import _telemetry
from .journal import CopyJournal
from .judges import AnswerJudges, Entity, Marker, ModelDecision, ModelJudge, Regex, ReviewedAnswers, TextAnswer
from .model import Drain, Hold
from .subject import Subject, Turn
from .v3 import Answer, Artifact, ExternalItem, Maintenance, SharedCase, Stored, observe_baseline, run_shared

__all__ = [
    "Answer", "AnswerJudges", "Artifact", "CopyJournal", "Drain", "Entity", "ExternalItem", "Hold",
    "Maintenance", "Marker", "ModelDecision", "ModelJudge", "Regex", "ReviewedAnswers", "SharedCase", "Stored", "Subject",
    "TextAnswer", "Turn", "observe_baseline", "run_shared",
]

_telemetry.configure()
