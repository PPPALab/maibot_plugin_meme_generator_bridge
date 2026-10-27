"""Shared fixtures for the dispatch-contract test suite."""

from pathlib import Path

import importlib

import pytest
from PIL import ImageFont


@pytest.fixture(autouse=True)
def installed_test_font(monkeypatch):
    # External font availability seam; actual CJK appearance checked in rendered samples.
    module = importlib.import_module("maibot_plugin_meme_generator_bridge.menu")
    monkeypatch.setattr(module, "FONT_PATHS", (str(Path(ImageFont.truetype("DejaVuSans.ttf", 22).path)),))
