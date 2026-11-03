"""Shared fixtures for the dispatch-contract test suite."""

from pathlib import Path

import importlib
import json

import pytest
from PIL import ImageFont


@pytest.fixture(autouse=True)
def installed_test_font(monkeypatch):
    # External font availability seam; actual CJK appearance checked in rendered samples.
    module = importlib.import_module("maibot_plugin_meme_generator_bridge.tools.build_menu")
    monkeypatch.setattr(module, "FONT_PATHS", (str(Path(ImageFont.truetype("DejaVuSans.ttf", 22).path)),))


@pytest.fixture
def bundled_items():
    path = Path(__file__).resolve().parents[1] / "assets/menu-v0.2.3.json"
    return json.loads(path.read_text(encoding="utf-8"))["templates"]
