"""Hypothesis strategies shared by tests across phases."""

import uuid

from hypothesis import strategies as st


def node_ids() -> st.SearchStrategy[str]:
    """Lowercase UUIDv7 strings with random bits (not time-ordered)."""
    return st.integers(0, 2**128 - 1).map(lambda n: str(uuid.UUID(int=n, version=7)))
