"""Vision tests exercise the model entry points; open the learned-model gate."""
import pytest


@pytest.fixture(autouse=True)
def _vision_models_enabled(monkeypatch):
    from mast.vision.module import reset_vision_gate
    monkeypatch.setenv("MAST_VISION_MODELS", "1")
    reset_vision_gate()
    yield
    reset_vision_gate()
