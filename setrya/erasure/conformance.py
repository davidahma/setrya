"""Destructive conformance fixture for contributors implementing direct adapters."""

from __future__ import annotations

from .adapters import ErasureAdapter
from .subject import Subject


def check_erasure_adapter(
    adapter: ErasureAdapter,
    *,
    subject: Subject,
    retained: Subject,
    expected: set[str],
    retained_expected: set[str],
) -> None:
    """Exercise two explicitly seeded, disposable subjects and erase only the first."""
    if (adapter.proof != "direct" or subject.ref == retained.ref or not expected or not retained_expected):
        raise ValueError("seed two distinct disposable subjects with direct evidence and nonempty expected keys")
    for selected, keys in ((subject, expected), (retained, retained_expected)):
        if adapter.find(selected) != keys or adapter.verify_absent(selected) != keys:
            raise ValueError("the direct observer must discover every seeded key before deletion")
    for _ in range(2):
        adapter.delete(subject)
        if adapter.find(subject) or adapter.verify_absent(subject):
            raise ValueError("the selected subject still has copies")
        if adapter.find(retained) != retained_expected or adapter.verify_absent(retained) != retained_expected:
            raise ValueError("deleting the selected subject changed the retained subject's copies")
