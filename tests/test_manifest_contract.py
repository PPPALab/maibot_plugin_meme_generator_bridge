"""Read-only host model validation without importing/starting the full MaiBot host.

Only its declarative models are executed; runtime imports and side effects are
excluded. This is not a host loading/integration test. Skips on standalone clones.
"""

from pathlib import Path

import ast
import json
import os

import pytest


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
    assert model.id == "local.meme-generator-bridge"
