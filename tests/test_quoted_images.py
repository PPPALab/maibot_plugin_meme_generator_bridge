"""Quoted-message image input via the official message.get_by_id capability."""

from unittest.mock import AsyncMock

import asyncio
import base64
import copy

import pytest
from maibot_sdk.context import PluginContext

from host_dispatch import Dispatch
from test_bridge import GIF, PNG, ITEM, message, plugin
from test_features import HAMMER, finish_jobs

from maibot_plugin_meme_generator_bridge.inputs import collect_images


async def test_real_sdk_unwrapped_message_can_supply_quoted_image():
    p = await plugin()
    rpc = AsyncMock(return_value=fetched())
    context = PluginContext("github.pppalab.meme-generator-bridge", rpc_call=rpc)
    try:
        images = await collect_images(context, p.client, quoted_message(), "stream")
        assert images == [PNG]
        assert rpc.await_count == 1
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "message": None},
        {"success": False, "error": "unavailable"},
        None,
        {"success": True, "message": {"session_id": "other-stream", "raw_message": []}},
    ],
)
async def test_real_sdk_missing_or_failed_quote_remains_consumed(payload):
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock()
    context = PluginContext("github.pppalab.meme-generator-bridge", rpc_call=AsyncMock(return_value=payload))
    p.ctx.message.get_by_id = context.message.get_by_id
    try:
        assert await p.handle_command(**cmd(quoted_message())) == (False, None, 1)
        p.client.generate.assert_not_awaited()
        p.ctx.send.text.assert_awaited_once()
    finally:
        await p.on_unload()


def encoded(data=PNG):
    return base64.b64encode(data).decode()


def fetched(segments=None, session="stream"):
    return {
        "success": True,
        "message": {
            "session_id": session,
            "raw_message": segments
            if segments is not None
            else [{"type": "image", "hash": "h", "binary_data_base64": encoded()}],
        },
    }


def cmd(m, text="/做表情 锤"):
    return {
        "text": text,
        "message": m,
        "stream_id": "stream",
        "group_id": "12345",
        "user_id": "23456",
        "platform": "qq",
    }


def quoted_message(target="q1", top_level=None, text="/做表情 锤"):
    m = message(text)
    m["raw_message"] = [{"type": "reply", "data": {"target_message_id": target}}, {"type": "text", "data": text}]
    if top_level:
        m["reply_to"] = top_level
    return m


@pytest.mark.parametrize("direct_message", [False, True])
async def test_quoted_image_only_generates_from_referenced_bytes(direct_message):
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    payload = fetched()
    p.ctx.message.get_by_id = AsyncMock(return_value=payload["message"] if direct_message else payload)
    try:
        result = await p.handle_command(**cmd(quoted_message()))
        assert result == (True, None, 1)
        p.ctx.message.get_by_id.assert_awaited_once_with(message_id="q1", chat_id="stream", include_binary_data=True)
        assert p.client.generate.await_args.args[1:] == ([], [PNG])
        p.ctx.send.emoji.assert_awaited_once()
    finally:
        await p.on_unload()


@pytest.mark.parametrize("entry", ["keyword", "slash"])
@pytest.mark.parametrize("at_first", [True, False])
@pytest.mark.parametrize("has_quote", [True, False])
@pytest.mark.parametrize("target_id", ["90000", "34567"])
async def test_quote_beats_mentions_and_unquoted_bot_avatar_works_in_both_orders(entry, at_first, has_quote, target_id):
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched()["message"])
    avatar = PNG + b"avatar"
    p.ctx.chat.get_avatar.return_value = {
        "status": "available",
        "url": f"https://q1.qlogo.cn/g?b=qq&nk={target_id}&s=640",
    }
    p.avatars.get = AsyncMock(return_value=avatar)
    text = "锤" if entry == "keyword" else "/做表情 锤"
    mention = {"type": "at", "data": {"target_user_id": target_id}}
    word = {"type": "text", "data": text}
    raw = [mention, word] if at_first else [word, mention]
    if has_quote:
        raw.insert(0, {"type": "reply", "data": {"target_message_id": "q1"}})
    dispatch = Dispatch(p)
    m = dispatch.message(text)
    m.raw_segments = raw
    try:
        await dispatch.bot.receive_message(m)
        await finish_jobs(p)
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        p.client.generate.assert_awaited_once()
        assert p.client.generate.await_args.args[1:] == ([], [PNG if has_quote else avatar])
        p.ctx.send.emoji.assert_awaited_once()
        if has_quote:
            p.ctx.message.get_by_id.assert_awaited_once_with(
                message_id="q1", chat_id="stream", include_binary_data=True
            )
            p.ctx.chat.get_avatar.assert_not_awaited()
            p.avatars.get.assert_not_awaited()
        else:
            p.ctx.message.get_by_id.assert_not_awaited()
            p.ctx.chat.get_avatar.assert_awaited_once()
            assert p.ctx.chat.get_avatar.await_args.kwargs["target_id"] == target_id
    finally:
        await p.on_unload()


async def test_unavailable_quote_never_falls_back_to_mentioned_avatar():
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock()
    p.ctx.message.get_by_id = AsyncMock(return_value=None)
    m = quoted_message()
    m["raw_message"].append({"type": "at", "data": {"target_user_id": "90000"}})
    try:
        assert await p.handle_command(**cmd(m)) == (False, None, 1)
        p.ctx.message.get_by_id.assert_awaited_once()
        p.ctx.chat.get_avatar.assert_not_awaited()
        p.client.generate.assert_not_awaited()
        assert "引用的图片已不可用" in p.ctx.send.text.await_args.args[0]
    finally:
        await p.on_unload()


async def test_current_image_wins_and_quote_is_never_fetched():
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched())
    current = [{"type": "image", "hash": "h", "binary_data_base64": encoded()}]
    m = quoted_message()
    m["raw_message"] = [
        *current,
        {"type": "reply", "data": {"target_message_id": "q1"}},
        {"type": "text", "data": "/做表情 锤"},
    ]
    try:
        assert await p.handle_command(**cmd(m)) == (True, None, 1)
        p.ctx.message.get_by_id.assert_not_awaited()
        assert p.client.generate.await_args.args[1:] == ([], [PNG])
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "m,target",
    [
        (quoted_message("from-reply"), "from-reply"),
        ({**message("/做表情 锤"), "reply_to": "from-top-level"}, "from-top-level"),
    ],
)
async def test_reply_component_and_top_level_targets(m, target):
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched())
    try:
        assert await p.handle_command(**cmd(m)) == (True, None, 1)
        assert p.ctx.message.get_by_id.await_args.kwargs["message_id"] == target
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "reply,error",
    [
        ({"success": False, "error": "boom"}, "引用的图片已不可用"),
        ({"success": True, "message": None}, "引用的图片已不可用"),
        (fetched([{"type": "image", "hash": "h"}]), "引用的图片已不可用"),
        (
            fetched(
                [{"type": "image", "hash": "h", "binary_data_base64": encoded(b"\x89PNG\r\n\x1a\n" + b"x" * 5000)}]
            ),
            "引用的图片不可用",
        ),
        (asyncio.TimeoutError(), "读取引用图片超时"),
    ],
)
async def test_quoted_failures_are_readable_consumed_and_retry_free(reply, error):
    p = await plugin(max_input_bytes=2048)
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock()
    p.ctx.message.get_by_id = (
        AsyncMock(side_effect=reply) if isinstance(reply, Exception) else AsyncMock(return_value=reply)
    )
    try:
        result = await p.handle_command(**cmd(quoted_message()))
        assert result == (False, None, 1)  # consumed, error reply
        p.client.generate.assert_not_awaited()
        p.ctx.send.text.assert_awaited_once()
        assert error in p.ctx.send.text.await_args.args[0]
        p.ctx.message.get_by_id.assert_awaited_once()
    finally:
        await p.on_unload()


async def test_quoted_session_mismatch_is_rejected():
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock()
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched(session="other-stream"))
    try:
        result = await p.handle_command(**cmd(quoted_message()))
        assert result == (False, None, 1)
        p.client.generate.assert_not_awaited()
        assert "不在当前会话" in p.ctx.send.text.await_args.args[0]
    finally:
        await p.on_unload()


async def test_quoted_total_bytes_limit_is_enforced():
    p = await plugin(max_input_bytes=4096, max_total_input_bytes=2000)
    p.client.resolve = AsyncMock(return_value=HAMMER)
    blob = encoded(b"\x89PNG\r\n\x1a\n" + b"y" * 1500)
    p.ctx.message.get_by_id = AsyncMock(
        return_value=fetched(
            [
                {"type": "image", "hash": "a", "binary_data_base64": blob},
                {"type": "image", "hash": "b", "binary_data_base64": blob},
            ]
        )
    )
    try:
        result = await p.handle_command(**cmd(quoted_message()))
        assert result == (False, None, 1)
        assert "总量超过限制" in p.ctx.send.text.await_args.args[0]
    finally:
        await p.on_unload()


async def test_quoted_switch_off_never_calls_host_and_reports_missing_input():
    p = await plugin(allow_quoted_images=False)
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched())
    try:
        result = await p.handle_command(**cmd(quoted_message()))
        assert result == (False, None, 1)
        p.ctx.message.get_by_id.assert_not_awaited()
        assert "图片数量要求" in p.ctx.send.text.await_args.args[0]
    finally:
        await p.on_unload()


async def test_text_only_template_ignores_quoted_image_entirely():
    p = await plugin()
    text_item = {
        **copy.deepcopy(ITEM),
        "key": "words",
        "keywords": ["填词"],
        "params": {
            "min_images": 0,
            "max_images": 0,
            "min_texts": 1,
            "max_texts": 1,
            "default_texts": [],
            "options": [],
        },
    }
    p.client.resolve = AsyncMock(return_value=text_item)
    p.client.generate = AsyncMock(return_value=(PNG, "png"))
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched())
    try:
        assert await p.handle_command(**cmd(quoted_message(text='/做表情 填词 "你好"'))) == (True, None, 1)
        p.ctx.message.get_by_id.assert_not_awaited()
        assert p.client.generate.await_args.args[1:] == (["你好"], [])
    finally:
        await p.on_unload()


async def test_keyword_path_uses_quoted_image_and_still_consumes():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    p.ctx.message.get_by_id = AsyncMock(return_value=fetched())
    dispatch = Dispatch(p)
    m = dispatch.message(
        "", media=[{"type": "reply", "data": {"target_message_id": "q9"}}, {"type": "text", "data": " 锤"}]
    )
    try:
        await dispatch.bot.receive_message(m)
        m.process.assert_not_awaited()
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        await finish_jobs(p)
        p.ctx.message.get_by_id.assert_awaited_once_with(message_id="q9", chat_id="stream", include_binary_data=True)
        assert p.client.generate.await_args.args[1:] == ([], [PNG])
    finally:
        await p.on_unload()


async def test_forward_segments_and_user_urls_stay_unsupported():
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock()
    p.ctx.message.get_by_id = AsyncMock()
    try:
        for raw in [
            [{"type": "forward", "data": "x"}, {"type": "text", "data": "/做表情 锤"}],
            [{"type": "image", "data": "https://example.invalid/a.png"}, {"type": "text", "data": "/做表情 锤"}],
        ]:
            m = {**message("/做表情 锤"), "raw_message": raw}
            assert await p.handle_command(**cmd(m)) == (False, None, 1)
            p.ctx.message.get_by_id.assert_not_awaited()
            p.ctx.send.text.reset_mock()
        p.client.generate.assert_not_awaited()
    finally:
        await p.on_unload()
