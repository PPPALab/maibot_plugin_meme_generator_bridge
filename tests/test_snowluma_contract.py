"""Opt-in, read-only HTTP source checks; never start/connect a QQ client.

Execute only selected audited codec methods at pinned upstream commits in memory.
No remote modules, repositories or source files are installed or cloned.
"""

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen
from unittest.mock import AsyncMock

from maibot_sdk import Command
from maibot_sdk.capabilities.chat import ChatCapability
from maibot_sdk.capabilities.send import SendCapability

import ast
import base64
import inspect
import os
import re
import textwrap

import pytest

from host_dispatch import methods
from test_bridge import GIF, ITEM, PNG, ctx, message, plugin, settings

from maibot_plugin_meme_generator_bridge.client import BridgeError, MemeClient
from maibot_plugin_meme_generator_bridge.inputs import collect_images


ADAPTER = "ab751ae5c11f501e0158e33cacc321b33e34ef0c"


@pytest.fixture(scope="module")
def adapter_sources():
    if os.environ.get("VERIFY_SNOWLUMA_SOURCE") != "1":
        pytest.skip("set VERIFY_SNOWLUMA_SOURCE=1 for pinned upstream source checks (no QQ connection)")
    paths = ["codecs/inbound/message_codec.py", "codecs/outbound/segment_encoder.py", "apis/account.py"]
    sources = {}
    for path in paths:
        url = f"https://raw.githubusercontent.com/Mai-with-u/MaiBot-SnowLuma-Adapter/{ADAPTER}/{path}"
        with urlopen(url, timeout=20) as response:
            body = response.read(200_001)
            assert len(body) <= 200_000
            sources[path] = body.decode("utf-8")
    return sources


def codec_namespace():
    return {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Mapping": Mapping,
        "Optional": Optional,
        "Tuple": Tuple,
        "QQPayload": Dict,
        "QQSegment": Dict,
        "QQSegments": List,
        "QQIncomingSegments": List,
        "base64": base64,
        "re": re,
        "hashlib": __import__("hashlib"),
    }


async def test_actual_snowluma_inbound_segments_and_sdk_hook(adapter_sources):
    source = adapter_sources["codecs/inbound/message_codec.py"]
    namespace = codec_namespace()
    codec_class = methods(
        source,
        "QQInboundCodec",
        [
            "_convert_incoming_segments",
            "_build_text_segment",
            "_build_image_like_segment",
        ],
        namespace,
    )
    codec = codec_class()
    codec._resolve_at_target_info = AsyncMock(return_value=("展示昵称", None))
    codec._normalize_numeric_segment_value = lambda x: int(x) if x is not None else None
    codec._encode_binary = lambda data: base64.b64encode(data).decode("ascii")
    codec._load_binary_from_segment_data = AsyncMock(return_value=PNG)
    raw, is_at = await codec._convert_incoming_segments(
        [
            {"type": "text", "data": {"text": "/做表情 test hello"}},
            {"type": "image", "data": {"base64": base64.b64encode(PNG).decode(), "sub_type": 0}},
            {"type": "at", "data": {"qq": "12345"}},
        ],
        "90000",
        "12345",
    )
    assert not is_at
    assert [item["type"] for item in raw] == ["text", "image", "at"]
    assert raw[-1]["data"]["target_user_id"] == "12345"
    assert raw[-1]["data"]["target_user_nickname"] == "展示昵称"
    p = await plugin()
    try:
        m = {
            **message(),
            "raw_message": raw,
            "platform": "qq",
            "account_id": "90000",
            "scope": None,
            "message_info": {"group_info": {"group_id": "12345"}},
        }
        await p.capture_current_media(message=m, command_name="meme_bridge")
        stripped = {**m, "raw_message": [{k: v for k, v in item.items() if k != "binary_data_base64"} for item in raw]}
        attached = p._attach_current_media(stripped, "stream")
        assert await collect_images(
            p.ctx, p.client, {**attached, "raw_message": attached["raw_message"][:2]}, "stream"
        ) == [PNG]
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "data,kind,capability,key,subtype",
    [
        (PNG, "png", "send.image", "image_base64", 0),
        (GIF, "gif", "send.emoji", "emoji_base64", 1),
    ],
)
async def test_actual_sdk_send_and_snowluma_encoding(adapter_sources, data, kind, capability, key, subtype):
    namespace = codec_namespace()
    encoder = methods(
        adapter_sources["codecs/outbound/segment_encoder.py"],
        "QQOutboundSegmentEncoder",
        [
            "_build_image_segments",
            "_build_emoji_segments",
        ],
        namespace,
    )()
    rpc = SimpleNamespace(call_capability=AsyncMock(return_value=True))
    p = await plugin()
    original_ctx = p.ctx
    p._set_context(SimpleNamespace(send=SendCapability(rpc), logger=original_ctx.logger))
    p.client.resolve = AsyncMock(return_value=ITEM)
    p.client.preview = AsyncMock(return_value=(data, kind))
    try:
        assert (
            await p.handle_command(
                text="/表情预览 test",
                message=message("/表情预览 test"),
                stream_id="stream",
                group_id="12345",
                user_id="23456",
                platform="qq",
            )
        )[2] == 1
        rpc.call_capability.assert_awaited_once()
        assert rpc.call_capability.await_args.args == (capability,)
        kwargs = rpc.call_capability.await_args.kwargs
        assert kwargs[key] == base64.b64encode(data).decode("ascii")
        assert kwargs["stream_id"] == "stream"
        # Host capabilities/core.py routes these SDK keys into image/emoji send service.
        # The adapter receives the serialized Host image/emoji segment, not this RPC dict.
        segment = {"type": "emoji" if kind == "gif" else "image", "binary_data_base64": kwargs[key]}
        onebot = encoder._build_emoji_segments(segment) if kind == "gif" else encoder._build_image_segments(segment)
        assert onebot[0]["type"] == "image"
        assert onebot[0]["data"]["file"] == "base64://" + kwargs[key]
        assert onebot[0]["data"]["sub_type"] == subtype
    finally:
        await p.on_unload()


async def test_snowluma_avatar_is_url_only_no_external_download(adapter_sources):
    # Keep upstream method body, remove its API decorator (registration is not under test).
    tree = ast.parse(adapter_sources["apis/account.py"])
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QQAccountApiMixin")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_get_avatar")
    method.decorator_list = []
    namespace = codec_namespace()
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "<avatar-contract>", "exec"),
        namespace,
    )
    provider = SimpleNamespace(_normalize_positive_int=lambda value, name: int(value))
    avatar = await namespace["api_get_avatar"](provider, "qq", "12345")
    assert avatar["url"] == "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"
    rpc = SimpleNamespace(call_capability=AsyncMock(return_value={**avatar, "expires_at": 0}))
    context = ctx()
    context.chat = ChatCapability(rpc)
    client = MemeClient(settings())
    try:
        m = message(media=[{"type": "at", "data": {"target_user_id": "12345"}}])
        with pytest.raises(BridgeError, match="仅提供头像 URL"):
            await collect_images(context, client, m, "stream")
        rpc.call_capability.assert_awaited_once_with(
            "chat.get_avatar",
            platform="qq",
            target_id="12345",
            target_type="user",
            account_id="",
            scope="",
            force_refresh=False,
        )
        assert not hasattr(client, "avatar")
    finally:
        await client.close()


def test_installed_sdk_matches_pinned_current_contract(adapter_sources):
    sdk_sha = "835e0e9d1f70ea00885fb49d059f7f2864976d05"
    checks = {
        "components.py": [Command],
        "capabilities/send.py": [SendCapability.text, SendCapability.image, SendCapability.emoji],
        "capabilities/chat.py": [ChatCapability.get_avatar],
    }
    for path, handlers in checks.items():
        url = f"https://raw.githubusercontent.com/Mai-with-u/maibot-plugin-sdk/{sdk_sha}/maibot_sdk/{path}"
        with urlopen(url, timeout=20) as response:
            body = response.read(200_001)
            assert len(body) <= 200_000
        tree = ast.parse(body.decode())
        for handler in handlers:
            upstream = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == handler.__name__
            )
            installed = ast.parse(textwrap.dedent(inspect.getsource(handler))).body[0]
            # inspect dedents a method's source, including its docstring spaces.
            # Compare executable AST and signatures, not docstring indentation.
            for selected in (upstream, installed):
                for node in ast.walk(selected):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                        first = node.body[0]
                        if (
                            isinstance(first, ast.Expr)
                            and isinstance(first.value, ast.Constant)
                            and isinstance(first.value.value, str)
                        ):
                            node.body.pop(0)
            assert ast.dump(upstream) == ast.dump(installed)
