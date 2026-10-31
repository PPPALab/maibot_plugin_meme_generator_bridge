"""Pinned release contracts, executed without starting a Host or reading user data."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import ast
import base64
import json
import subprocess

from maibot_sdk.context import PluginContext
import tomli

from host_dispatch import Dispatch, host_root, methods
from test_bridge import PNG, plugin
from test_quoted_images import HAMMER, finish_jobs
from maibot_plugin_meme_generator_bridge.plugin import create_plugin


RELEASES = {
    "1.3.15": "062d45cf8bd4272ac57d7ec03fcf405e5a458211",
    "1.3.19": "c6429547af711555e3d4d1b8d04b413572cb616c",
}


def release_version():
    host = host_root()
    version = tomli.loads((host / "pyproject.toml").read_text())["project"]["version"]
    assert version in RELEASES, "Use an explicitly audited Host release"
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=host, text=True).strip()
    assert sha == RELEASES[version], "Do not substitute moving main for a release"
    return version


def test_manifest_declares_only_audited_target_pair():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "_manifest.json").read_text())
    assert manifest["host_application"] == {"min_version": "1.3.19", "max_version": "1.3.19"}
    assert manifest["sdk"] == {"min_version": "2.20.0", "max_version": "2.20.0"}


async def test_real_sdk_media_send_and_message_envelopes():
    payload = {"success": True}
    rpc = AsyncMock(side_effect=lambda *args, **kw: payload)
    ctx = PluginContext("github.pppalab.meme-generator-bridge", rpc_call=rpc)
    for method in (ctx.send.text, ctx.send.image, ctx.send.emoji):
        assert await method("synthetic", "stream") is True
    payload = {"success": False}
    assert await ctx.send.image("synthetic", "stream") is False
    message = {"session_id": "stream", "raw_message": []}
    payload = {"success": True, "message": message}
    assert await ctx.message.get_by_id(message_id="q", chat_id="stream", include_binary_data=True) == message
    assert rpc.await_args.args[2]["args"]["include_binary_data"] is True
    payload = {"success": True, "avatar": {"status": "available", "url": "https://q1.qlogo.cn/g"}}
    assert (await ctx.chat.get_avatar(platform="qq", target_id="12345", account_id="90000", scope="route"))[
        "status"
    ] == "available"


def test_pinned_release_factory_and_hook_specs():
    release_version()
    host = host_root()
    namespace = {"Any": Any}
    loader = methods(
        (host / "src/plugin_runtime/runner/plugin_loader.py").read_text(),
        "PluginLoader",
        ["_validate_sdk_plugin_contract"],
        namespace,
    )
    p = create_plugin()
    loader._validate_sdk_plugin_contract("github.pppalab.meme-generator-bridge", p)
    assert p.get_config_reload_subscriptions() == []
    components = p.get_components()
    command = next(c for c in components if c["type"] == "COMMAND")
    assert command["metadata"]["intercept_message_level"] == 1
    # Execute the official Hook registration function, not just a string search.
    source = ast.parse((host / "src/chat/message_receive/bot.py").read_text())
    function = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "register_chat_hook_specs")
    namespace = {
        "HookSpecRegistry": Any,
        "List": List,
        "HookSpec": lambda **kw: SimpleNamespace(**kw),
        "build_object_schema": lambda *a, **kw: {},
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "<hook-spec>", "exec"),
        namespace,
    )
    specs = namespace["register_chat_hook_specs"](SimpleNamespace(register_hook_specs=lambda specs: specs))
    for name in ("chat.receive.before_process", "chat.command.before_execute"):
        spec = next(s for s in specs if s.name == name)
        assert spec.allow_abort and spec.allow_kwargs_mutation


async def test_keyword_abort_precedes_file_ingest_and_normal_message_continues():
    version = release_version()
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    dispatch = Dispatch(p)
    try:
        m = dispatch.message("锤")
        await dispatch.bot.receive_message(m)
        await finish_jobs(p)
        m.process.assert_not_awaited()
        dispatch.file_service.ingest.assert_not_awaited()
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        ordinary = dispatch.message("普通消息")
        await dispatch.bot.receive_message(ordinary)
        dispatch.bot.heartflow_message_receiver.process_message.assert_awaited_once_with(ordinary)
        if version == "1.3.19":
            dispatch.file_service.ingest.assert_awaited_once_with(ordinary)
        else:
            dispatch.file_service.ingest.assert_not_awaited()
    finally:
        await p.on_unload()


def test_actual_host_serialization_preserves_route_reply_mentions_and_binary():
    release_version()
    # Component objects are synthetic; actual Host serializer code runs unchanged.
    classes = {
        name: type(name, (), {})
        for name in (
            "TextComponent",
            "ImageComponent",
            "EmojiComponent",
            "VoiceComponent",
            "FileComponent",
            "AtComponent",
            "ReplyComponent",
            "ForwardNodeComponent",
        )
    }
    ns = {
        **classes,
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "SessionMessage": Any,
        "MessageDict": dict,
        "MessageInfoDict": dict,
        "UserInfoDict": dict,
        "GroupInfoDict": dict,
        "MessageSequence": Any,
        "MessageInfo": Any,
        "StandardMessageComponents": Any,
        "base64": base64,
    }
    utils = methods(
        (host_root() / "src/plugin_runtime/host/message_utils.py").read_text(),
        "PluginMessageUtils",
        [
            "_session_message_to_dict",
            "_message_info_to_dict",
            "_message_sequence_to_dict",
            "_component_to_dict",
            "_binary_component_to_base64",
        ],
        ns,
    )

    def component(name, **values):
        value = classes[name]()
        value.__dict__.update(values)
        return value

    raw = [
        component(
            "ReplyComponent",
            target_message_id="quoted",
            target_message_content=None,
            target_message_sender_id=None,
            target_message_sender_nickname=None,
            target_message_sender_cardname=None,
        ),
        component("AtComponent", target_user_id="12345", target_user_nickname="test", target_user_cardname=None),
        component("ImageComponent", binary_data=PNG, binary_hash="synthetic", content=""),
    ]
    message = SimpleNamespace(
        message_id="synthetic",
        timestamp=datetime(2026, 1, 1),
        platform="qq",
        account_id="90000",
        scope="route",
        message_info=SimpleNamespace(
            user_info=SimpleNamespace(user_id="12345", user_nickname="test", user_cardname=None),
            group_info=None,
            additional_config={},
        ),
        raw_message=SimpleNamespace(components=raw),
        is_mentioned=False,
        is_at=False,
        is_emoji=False,
        is_picture=True,
        is_command=False,
        is_notify=False,
        session_id="stream",
        received_at=None,
        reply_to="quoted",
        processed_plain_text="锤",
    )
    data = utils._session_message_to_dict(message)
    assert (data["account_id"], data["scope"], data["reply_to"], data["session_id"]) == (
        "90000",
        "route",
        "quoted",
        "stream",
    )
    assert data["raw_message"][0]["data"]["target_message_id"] == "quoted"
    assert data["raw_message"][1]["data"]["target_user_id"] == "12345"
    assert base64.b64decode(data["raw_message"][2]["binary_data_base64"]) == PNG
    without_binary = utils._session_message_to_dict(message, include_binary_data=False)
    assert "binary_data_base64" not in without_binary["raw_message"][2]
