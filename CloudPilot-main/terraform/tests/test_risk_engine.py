"""Regression tests for configuration-driven service criticality."""
import inspect

from app import engines


def test_risk_engine_does_not_embed_service_names():
    source = inspect.getsource(engines)
    assert "checkout" not in source.lower()
