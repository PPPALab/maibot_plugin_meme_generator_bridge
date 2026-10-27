"""Read-only host model validation without importing/starting the full MaiBot host.

Only its declarative models are executed; runtime imports and side effects are
excluded. This is not a host loading/integration test. Skips on standalone clones.
"""

from pathlib import Path

import ast
import json
import os

import pytest


def test_release_identity_and_local_changelog():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "_manifest.json").read_text())
    assert manifest["id"] == "github.pppalab.meme-generator-bridge"
    assert manifest["name"] == "表情包制作桥接器"
    assert manifest["version"] == "0.1.2"
    assert manifest["plugin_type"] == "media"
    assert manifest["capabilities"] == [
        "send.text",
        "send.image",
        "send.emoji",
        "chat.get_avatar",
        "chat.open_session",
        "message.get_by_id",
    ]
    assert manifest["i18n"]["default_locale"] == "zh-CN"
    assert manifest["i18n"]["supported_locales"] == ["zh-CN"]
    assert "locales_path" not in manifest["i18n"]
    assert not (root / "_locales").exists()
    assert manifest["urls"]["repository"] == "https://github.com/PPPALab/maibot_plugin_meme_generator_bridge"
    assert manifest["changelog"] == "CHANGELOG.md"
    assert (root / manifest["changelog"]).is_file()
    assert "## 0.1.2" in (root / manifest["changelog"]).read_text()


def test_reference_host_manifest_models():
    root = Path(__file__).resolve().parents[1]
    host = Path(os.environ.get("MAIBOT_REFERENCE", root.parent / "reference" / "MaiBot"))
    source = host / "src/plugin_runtime/runner/manifest_validator.py"
    if not source.is_file():
        pytest.skip("set MAIBOT_REFERENCE to validate against the inspected host models")
    nodes = []
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.ClassDef) and node.name == "ManifestValidator":
            break
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("src")):
            continue
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "logger" for x in node.targets):
            continue
        if isinstance(node, ast.Import) and any(x.name == "tomllib" for x in node.names):
            node = ast.Import(names=[ast.alias(name="tomli", asname="tomllib")])
        nodes.append(node)
    namespace = {"__name__": "manifest_contract"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), namespace)
    model = namespace["PluginManifest"].model_validate(json.loads((root / "_manifest.json").read_text()))
    assert model.id == "github.pppalab.meme-generator-bridge"
    assert model.version == "0.1.2"
    assert model.plugin_type == "media"
    assert model.i18n.locales_path is None
    assert model.changelog == "CHANGELOG.md"
    assert (root / model.changelog).is_file()
    assert model.urls.documentation == (
        "https://github.com/PPPALab/maibot_plugin_meme_generator_bridge/blob/main/README.md"
    )
    assert model.urls.issues == "https://github.com/PPPALab/maibot_plugin_meme_generator_bridge/issues"
