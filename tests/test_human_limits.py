"""Human-readable resource units, legacy migration and exact byte equivalence."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional, Tuple

import ast
import copy
import json
import logging
import math

import httpx
import pytest
import tomli

from pydantic import ValidationError

from maibot_plugin_meme_generator_bridge.client import BridgeError, MemeClient
from maibot_plugin_meme_generator_bridge.config import AvatarSettings, BridgeConfig, MenuSettings, Settings
from maibot_plugin_meme_generator_bridge.plugin import create_plugin


def test_decimal_tail_cannot_round_to_an_integer_byte():
    with pytest.raises(ValidationError, match="精确到"):
        Settings(max_input_mb="0.5000000000000000000000000000000000000000000000001")


def test_invalid_legacy_limit_is_rejected_even_with_new_field():
    with pytest.raises(ValidationError):
        Settings(max_input_bytes=-1, max_input_mb=0.5)


def test_host_upgrade_preserves_legacy_limits_and_rejects_invalid_types():
    runner = host_config_runner()
    plugin = create_plugin()
    raw = {
        "plugin": {"config_version": "1.4.0", "max_input_bytes": 1024},
        "menu": {"max_pixels": 10001},
        "avatar": {"cache_bytes": 1234567},
    }
    result = runner._normalize_plugin_config(plugin, raw, suppress_errors=False)
    assert result.should_persist
    assert result.normalized_config["plugin"]["config_version"] == "1.5.0"
    cfg = BridgeConfig.model_validate(result.normalized_config)
    assert cfg.plugin.max_input_bytes == 1024
    assert cfg.menu.max_pixels == 10001
    assert cfg.avatar.cache_bytes == 1234567
    assert "max_input_bytes" not in result.normalized_config["plugin"]
    for invalid in ("1024", 1024.5, True, -1):
        broken = copy.deepcopy(raw)
        broken["plugin"]["max_input_bytes"] = invalid
        with pytest.raises(ValueError):
            runner._normalize_plugin_config(plugin, broken, suppress_errors=False)


@pytest.mark.parametrize(
    "model,old,new,scale,default,minimum,maximum",
    [
        (Settings, "max_input_bytes", "max_input_mb", 1000000, 4000000, 1024, 8000000),
        (Settings, "max_total_input_bytes", "max_total_input_mb", 1000000, 8000000, 1024, 32000000),
        (Settings, "max_request_bytes", "max_request_mb", 1000000, 6000000, 1024, 16000000),
        (Settings, "max_operation_request_bytes", "max_operation_request_mb", 1000000, 12000000, 1024, 48000000),
        (Settings, "max_output_bytes", "max_output_mb", 1000000, 6000000, 1024, 12000000),
        (Settings, "max_metadata_bytes", "max_metadata_mb", 1000000, 4000000, 1024, 16000000),
        (Settings, "max_total_text_bytes", "max_total_text_kb", 1000, 12000, 1, 64000),
        (MenuSettings, "max_pixels", "max_pixels_wan", 10000, 4000000, 10000, 8000000),
        (AvatarSettings, "cache_bytes", "cache_mb", 1000000, 8000000, 1024, 32000000),
    ],
)
def test_all_limits_exact_defaults_boundaries_and_save_reload(model, old, new, scale, default, minimum, maximum):
    assert getattr(model(), old) == default
    for count in (minimum, minimum + 1, default, maximum - 1, maximum, 12345):
        if not minimum <= count <= maximum:
            continue
        instance = model(**{old: count})
        assert getattr(instance, old) == count
        assert Decimal(str(getattr(instance, new))) * scale == count
        for _ in range(3):
            instance = model.model_validate(instance.model_dump(mode="json"))
            assert getattr(instance, old) == count
    for count in (minimum - 1, maximum + 1):
        with pytest.raises(ValidationError):
            model(**{old: count})
        with pytest.raises(ValidationError):
            model(**{new: str(Decimal(count) / scale)})
    instance = model()
    before = instance.model_dump()
    with pytest.raises(ValidationError):
        setattr(instance, new, -1)
    assert instance.model_dump() == before


def host_config_runner():
    """Execute actual read-only Host config methods, without importing its runtime."""
    from host_dispatch import host_root, methods

    source = (host_root() / "src/plugin_runtime/runner/runner_main.py").read_text()
    tree = ast.parse(source)
    from maibot_sdk.config import PluginConfigVersionError

    namespace = dict(
        Any=Any,
        Dict=Dict,
        Mapping=Mapping,
        Tuple=Tuple,
        Optional=Optional,
        PluginConfigVersionError=PluginConfigVersionError,
        cast=lambda t, x: x,
        _ConfigAwarePlugin=Any,
        logger=logging.getLogger("config-contract"),
        compare_versions=lambda a, b: tuple(map(int, a.split("."))) < tuple(map(int, b.split("."))),
        PluginConfigNormalizationResult=lambda **kw: SimpleNamespace(**kw),
    )
    names = {
        "_deep_copy_plugin_config_value",
        "_deep_copy_plugin_config_mapping",
        "_is_plugin_config_value_type_compatible",
        "_overlay_plugin_config_fields",
        "extract_plugin_config_version",
        "rebuild_plugin_config_data",
        "_restore_host_reserved_plugin_config",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "<host-config>", "exec"), namespace
    )
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(n, ast.FunctionDef) and n.name == "_normalize_plugin_config" for n in node.body)
    )
    runner_cls = methods(
        source,
        class_node.name,
        {"_normalize_plugin_config", "_prepare_plugin_config_for_version_update", "_get_plugin_default_config"},
        namespace,
    )
    return runner_cls()


def test_defaults_match_legacy_limits_exactly():
    settings = Settings()
    assert settings.max_input_mb == 4.0
    assert settings.max_input_bytes == 4_000_000
    assert settings.max_total_input_mb == 8.0
    assert settings.max_total_input_bytes == 8_000_000
    assert settings.max_request_mb == 6.0
    assert settings.max_request_bytes == 6_000_000
    assert settings.max_operation_request_mb == 12.0
    assert settings.max_operation_request_bytes == 12_000_000
    assert settings.max_output_mb == 6.0
    assert settings.max_output_bytes == 6_000_000
    assert settings.max_metadata_mb == 4.0
    assert settings.max_metadata_bytes == 4_000_000
    assert settings.max_total_text_kb == 12.0
    assert settings.max_total_text_bytes == 12_000

    menu = MenuSettings()
    assert menu.max_pixels_wan == 400.0
    assert menu.max_pixels == 4_000_000

    avatar = AvatarSettings()
    assert avatar.cache_mb == 8.0
    assert avatar.cache_bytes == 8_000_000


def test_legacy_bytes_migrate_exactly_and_are_not_dumped():
    settings = Settings(
        max_input_bytes=1024,
        max_total_input_bytes=1_234_567,
        max_request_bytes=1025,
        max_operation_request_bytes=1026,
        max_output_bytes=1027,
        max_metadata_bytes=1028,
        max_total_text_bytes=12_000,
    )
    assert settings.max_input_mb == 0.001024
    assert settings.max_total_input_mb == 1.234567
    assert settings.max_request_mb == 0.001025
    assert settings.max_operation_request_mb == 0.001026
    assert settings.max_output_mb == 0.001027
    assert settings.max_metadata_mb == 0.001028
    assert settings.max_total_text_kb == 12.0
    assert settings.max_input_bytes == 1024
    assert settings.max_total_input_bytes == 1_234_567
    assert settings.max_total_text_bytes == 12_000

    dump = settings.model_dump()
    for legacy in (
        "max_input_bytes",
        "max_total_input_bytes",
        "max_request_bytes",
        "max_operation_request_bytes",
        "max_output_bytes",
        "max_metadata_bytes",
        "max_total_text_bytes",
    ):
        assert legacy not in dump


def test_legacy_menu_and_avatar_migrate_exactly():
    menu = MenuSettings(max_pixels=7_000_000)
    assert menu.max_pixels_wan == 700.0
    assert menu.max_pixels == 7_000_000
    assert "max_pixels" not in menu.model_dump()

    avatar = AvatarSettings(cache_bytes=1_234_567)
    assert avatar.cache_mb == 1.234567
    assert avatar.cache_bytes == 1_234_567
    assert "cache_bytes" not in avatar.model_dump()


def test_new_decimal_units_convert_without_rounding():
    settings = Settings(max_input_mb=0.5, max_total_text_kb=0.001)
    assert settings.max_input_bytes == 500_000
    assert settings.max_total_text_bytes == 1

    menu = MenuSettings(max_pixels_wan=1.5)
    assert menu.max_pixels == 15_000

    avatar = AvatarSettings(cache_mb=0.5)
    assert avatar.cache_bytes == 500_000


def test_new_explicit_value_wins_but_legacy_wins_over_new_default():
    settings = Settings(max_input_bytes=1024, max_input_mb=0.5)
    assert settings.max_input_bytes == 500_000
    assert "max_input_bytes" not in settings.model_dump()

    legacy = Settings(max_input_bytes=1024, max_input_mb=4.0)
    assert legacy.max_input_bytes == 1024
    saved = legacy.model_dump()
    saved["max_input_mb"] = 4.0
    assert Settings.model_validate(saved).max_input_bytes == 4000000


def test_legacy_and_new_bounds_are_strict():
    with pytest.raises(ValidationError, match="单图上限"):
        Settings(max_input_bytes=1023)
    with pytest.raises(ValidationError, match="单图上限"):
        Settings(max_input_mb=0)
    with pytest.raises(ValidationError, match="单图上限"):
        Settings(max_input_mb=0.0001)
    with pytest.raises(ValidationError, match="单图上限"):
        Settings(max_input_mb=8.000001)
    with pytest.raises(ValidationError, match="文字总量上限"):
        Settings(max_total_text_kb=0.0005)
    with pytest.raises(ValidationError, match="文字总量上限"):
        Settings(max_total_text_bytes=64_001)
    with pytest.raises(ValidationError, match="万像素"):
        MenuSettings(max_pixels_wan=0.5)
    with pytest.raises(ValidationError, match="万像素"):
        MenuSettings(max_pixels_wan=800.1)
    with pytest.raises(ValidationError, match="头像缓存上限"):
        AvatarSettings(cache_mb=0.0005)


def test_non_finite_and_non_integer_values_are_rejected():
    with pytest.raises(ValidationError, match="有限数字"):
        Settings(max_input_mb=math.nan)
    with pytest.raises(ValidationError, match="有限数字"):
        Settings(max_input_mb=math.inf)
    with pytest.raises(ValidationError, match="1 字节"):
        Settings(max_input_mb=0.5000001)
    with pytest.raises(ValidationError, match="整数 bytes"):
        Settings(max_input_bytes=1024.0)
    with pytest.raises(ValidationError, match="整数 bytes"):
        Settings(max_input_bytes=True)


def test_legacy_properties_remain_writable_for_existing_callers():
    settings = Settings()
    settings.max_input_bytes = 1024
    assert settings.max_input_mb == 0.001024
    settings.max_input_mb = 0.5
    assert settings.max_input_bytes == 500_000

    menu = MenuSettings()
    menu.max_pixels = 7_000_000
    assert menu.max_pixels_wan == 700.0

    avatar = AvatarSettings()
    avatar.cache_bytes = 1_234_567
    assert float(avatar.cache_mb) == 1.234567


def test_model_dump_reload_has_no_drift():
    first = Settings(
        max_input_bytes=1_234_567,
        max_total_input_bytes=7_654_321,
        max_request_bytes=3_456_789,
        max_operation_request_bytes=9_876_543,
        max_output_bytes=1_111_111,
        max_metadata_bytes=2_222_222,
        max_total_text_bytes=12_345,
    )
    second = Settings.model_validate(first.model_dump())
    assert second.max_input_bytes == first.max_input_bytes
    assert second.max_total_input_bytes == first.max_total_input_bytes
    assert second.max_request_bytes == first.max_request_bytes
    assert second.max_operation_request_bytes == first.max_operation_request_bytes
    assert second.max_output_bytes == first.max_output_bytes
    assert second.max_metadata_bytes == first.max_metadata_bytes
    assert second.max_total_text_bytes == first.max_total_text_bytes


def test_default_config_keeps_legacy_skeleton_for_host_upgrade():
    plugin = create_plugin()
    defaults = plugin.get_default_config()
    assert defaults["plugin"]["max_input_bytes"] is None
    assert defaults["plugin"]["max_total_text_bytes"] is None
    assert defaults["menu"]["max_pixels"] is None
    assert defaults["avatar"]["cache_bytes"] is None

    plugin.set_plugin_config(defaults)
    saved = plugin.get_plugin_config_data()
    assert saved["plugin"]["config_version"] == "1.5.0"
    assert "max_input_bytes" not in saved["plugin"]
    assert "max_pixels" not in saved["menu"]
    assert "cache_bytes" not in saved["avatar"]


def test_plugin_normalizes_legacy_config_and_saves_human_units():
    plugin = create_plugin()
    plugin.set_plugin_config(
        {
            "plugin": {
                "config_version": "1.4.0",
                "enabled": True,
                "base_url": "http://engine:2233",
                "max_input_bytes": 1024,
                "max_total_text_bytes": 12_000,
            },
            "menu": {"max_pixels": 7_000_000},
            "avatar": {"cache_bytes": 1_234_567},
        }
    )
    assert plugin.config.plugin.max_input_bytes == 1024
    assert plugin.config.plugin.max_total_text_bytes == 12_000
    assert plugin.config.menu.max_pixels == 7_000_000
    assert plugin.config.avatar.cache_bytes == 1_234_567

    saved = plugin.get_plugin_config_data()
    assert "max_input_bytes" not in saved["plugin"]
    assert "max_total_text_bytes" not in saved["plugin"]
    assert "max_pixels" not in saved["menu"]
    assert "cache_bytes" not in saved["avatar"]
    assert saved["plugin"]["max_input_mb"] == 0.001024
    assert saved["plugin"]["max_total_text_kb"] == 12.0
    assert saved["menu"]["max_pixels_wan"] == 700.0
    assert saved["avatar"]["cache_mb"] == 1.234567

    plugin.set_plugin_config(saved)
    assert plugin.config.plugin.max_input_bytes == 1024
    assert plugin.config.menu.max_pixels == 7_000_000
    assert plugin.config.avatar.cache_bytes == 1_234_567


def test_schema_exposes_short_unit_labels_and_hides_legacy_fields():
    plugin = create_plugin()
    sections = plugin.get_webui_config_schema()["sections"]
    plugin_fields = sections["plugin"]["fields"]
    menu_fields = sections["menu"]["fields"]
    avatar_fields = sections["avatar"]["fields"]

    expected = {
        "max_input_mb": "单图上限 (MB)",
        "max_total_input_mb": "全部输入图上限 (MB)",
        "max_request_mb": "单次请求上限 (MB)",
        "max_operation_request_mb": "整次累计请求上限 (MB)",
        "max_output_mb": "成品上限 (MB)",
        "max_metadata_mb": "元数据上限 (MB)",
        "max_total_text_kb": "文字总量上限 (KB)",
    }
    for name, label in expected.items():
        assert plugin_fields[name]["label"] == label
        assert plugin_fields[name]["min"] is not None
        assert plugin_fields[name]["max"] is not None
        assert plugin_fields[name]["step"] == (0.001 if name.endswith("_kb") else 0.000001)
        assert plugin_fields[name]["hint"] == plugin_fields[name]["description"]
        assert (
            "1 MB=1,000,000" in plugin_fields[name]["description"] or "1 KB=1,000" in plugin_fields[name]["description"]
        )

    assert menu_fields["max_pixels_wan"]["label"] == "索引图上限 (万像素)"
    assert avatar_fields["cache_mb"]["label"] == "头像缓存上限 (MB)"
    for legacy in (
        "max_input_bytes",
        "max_total_input_bytes",
        "max_request_bytes",
        "max_operation_request_bytes",
        "max_output_bytes",
        "max_metadata_bytes",
        "max_total_text_bytes",
        "max_pixels",
        "cache_bytes",
    ):
        assert legacy not in plugin_fields
        assert legacy not in menu_fields
        assert legacy not in avatar_fields


def test_bridge_config_roundtrip_keeps_all_limits():
    config = BridgeConfig(
        plugin=Settings(max_input_bytes=1024, max_total_text_bytes=1),
        menu=MenuSettings(max_pixels=10_000),
        avatar=AvatarSettings(cache_bytes=1024),
    )
    dumped = config.model_dump()
    assert "max_input_bytes" not in dumped["plugin"]
    assert "max_total_text_bytes" not in dumped["plugin"]
    assert "max_pixels" not in dumped["menu"]
    assert "cache_bytes" not in dumped["avatar"]
    loaded = BridgeConfig.model_validate(dumped)
    assert loaded.plugin.max_input_bytes == 1024
    assert loaded.plugin.max_total_text_bytes == 1
    assert loaded.menu.max_pixels == 10_000
    assert loaded.avatar.cache_bytes == 1024


def test_legacy_toml_to_normalized_toml_roundtrip(tmp_path):
    """Real TOML reads; scalar TOML emission is a test seam, not Host file IO."""
    legacy = tomli.loads("""
[plugin]
config_version = "1.4.0"
enabled = true
base_url = "http://engine:2233"
max_input_bytes = 1024
max_total_input_bytes = 1234567
max_total_text_bytes = 12000
[menu]
max_pixels = 7000001
[avatar]
cache_bytes = 1234567
""")
    p = create_plugin()
    p.set_plugin_config(legacy)
    normalized = p.get_plugin_config_data()
    # These scalar/list values have the same JSON and TOML literal spelling.
    text = "\n".join(
        f"[{section}]\n"
        + "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
        for section, values in normalized.items()
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    with path.open("rb") as handle:
        saved = tomli.load(handle)
    p.set_plugin_config(saved)
    assert p.get_plugin_config_data() == normalized
    assert p.config.plugin.max_input_bytes == 1024
    assert p.config.plugin.max_total_input_bytes == 1234567
    assert p.config.plugin.max_total_text_bytes == 12000
    assert p.config.menu.max_pixels == 7000001
    assert p.config.avatar.cache_bytes == 1234567


def test_validation_errors_show_human_units():
    with pytest.raises(ValidationError) as input_error:
        Settings(max_input_bytes=1023)
    assert "1.024 KB" in str(input_error.value) and "8 MB" in str(input_error.value)

    with pytest.raises(ValidationError) as text_error:
        Settings(max_total_text_bytes=64_001)
    assert "64 KB" in str(text_error.value)

    with pytest.raises(ValidationError) as pixels_error:
        MenuSettings(max_pixels_wan=800.1)
    assert "800 万像素" in str(pixels_error.value)


async def test_runtime_input_error_shows_human_limit():
    client = MemeClient(
        Settings(max_input_bytes=1024),
        httpx.MockTransport(lambda request: pytest.fail("must fail before HTTP")),
    )
    item = {
        "key": "test",
        "keywords": ["test"],
        "params": {"min_images": 0, "max_images": 1, "min_texts": 0, "max_texts": 0},
    }
    try:
        with pytest.raises(BridgeError, match=r"单图上限 1\.024 KB"):
            await client.generate(item, [], [b"\x89PNG\r\n\x1a\n" + b"x" * 2000])
    finally:
        await client.close()


async def test_unit_limits_survive_actual_plugin_hot_update():
    from test_bridge import plugin as loaded_plugin

    p = await loaded_plugin(max_input_bytes=1024)
    before = p.client
    updated = p.get_plugin_config_data()
    updated["plugin"]["max_input_mb"] = 0.5
    updated["plugin"]["max_total_text_kb"] = 12.345
    updated["menu"]["max_pixels_wan"] = 700.0001
    updated["avatar"]["cache_mb"] = 1.234567
    try:
        await p.on_config_update("self", updated, "1.5.0")
        assert before.http.is_closed
        assert p.client.settings.max_input_bytes == 500000
        assert p.client.settings.max_total_text_bytes == 12345
        assert p.config.menu.max_pixels == 7000001
        assert p.avatars.settings.cache_bytes == 1234567
        assert p.config.plugin.allowed_groups == ["12345"]
        saved = p.get_plugin_config_data()
        assert saved["plugin"]["config_version"] == "1.5.0"
        assert "max_input_bytes" not in saved["plugin"]
        p.set_plugin_config(saved)
        assert p.config.plugin.max_input_bytes == 500000
    finally:
        await p.on_unload()
