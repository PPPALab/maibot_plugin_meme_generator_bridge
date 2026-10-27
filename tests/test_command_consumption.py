"""Actual SDK declarations + actual Host dispatch methods; external boundaries are doubles."""

from unittest.mock import AsyncMock

import asyncio
import base64
import copy
import time

import httpx
import pytest

from host_dispatch import Dispatch
from test_bridge import GIF, ID, ITEM, PNG, plugin

from maibot_plugin_meme_generator_bridge.client import MemeClient


@pytest.mark.parametrize(
    "case",
    [
        "help",
        "list",
        "details",
        "preview_png",
        "preview_gif",
        "generate_text",
        "generate_image",
        "missing_args",
        "bad_quotes",
        "bad_page",
        "unknown",
        "ambiguous",
        "disabled_template",
        "disabled_plugin",
        "group_denied",
        "private_denied",
        "wrong_platform",
        "closing",
        "missing_user",
        "cooldown",
        "queue_full",
        "queue_timeout",
        "operation_timeout",
        "service_timeout",
        "offline",
        "server_error",
        "bad_image_count",
        "missing_binary",
        "unsupported_at",
        "send_false",
        "send_exception",
        "error_send_exception",
        "unexpected_handler_error",
        "runner_exception",
        "runner_non_tuple",
        "rpc_exception",
    ],
)
async def test_every_command_outcome_is_consumed_before_llm(case):
    p = await plugin(concurrency=1, max_waiters=0, queue_timeout=0.1, operation_timeout=1)
    await p.client.close()
    infos = [copy.deepcopy(ITEM)]
    if case == "ambiguous":
        infos.append({**copy.deepcopy(ITEM), "key": "other"})

    def handler(request):
        if case == "service_timeout":
            raise httpx.ReadTimeout("private", request=request)
        if case == "offline":
            raise httpx.ConnectError("private", request=request)
        if case == "server_error":
            return httpx.Response(500, json={"code": 530, "message": "private"})
        if request.url.path == "/meme/infos":
            return httpx.Response(200, json=infos)
        if request.url.path.startswith("/image/") and request.method == "GET":
            return httpx.Response(200, content=GIF if case == "preview_gif" else PNG)
        return httpx.Response(200, json={"image_id": ID})

    p.client = MemeClient(p.config.plugin, httpx.MockTransport(handler))
    text = {
        "help": "/表情帮助",
        "list": "/表情列表",
        "details": "/表情详情 test",
        "generate_text": '/做表情 test "hello world"',
        "generate_image": "/做表情 test hello",
        "missing_args": "/做表情",
        "bad_quotes": '/做表情 test "broken',
        "bad_page": "/表情列表 no",
        "unknown": "/表情详情 unknown",
        "ambiguous": "/表情详情 测试",
        "bad_image_count": "/做表情 test",
        "missing_binary": "/做表情 test hello",
        "unsupported_at": "/做表情 test hello",
        "error_send_exception": "/做表情",
    }.get(case, "/表情预览 test")
    media = []
    if case == "generate_image":
        media = [{"type": "image", "hash": "hash", "binary_data_base64": base64.b64encode(PNG).decode()}]
    if case == "missing_binary":
        media = [{"type": "image", "hash": "hash"}]
    if case == "unsupported_at":
        media = [{"type": "at", "data": {"target_user_id": "12345"}}]
        p.ctx.chat.get_avatar.return_value = {"status": "available", "url": "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"}
        p.avatars.get = AsyncMock(side_effect=RuntimeError("avatar unavailable"))
    if case == "disabled_template":
        p.client.settings.disabled_templates = ["test"]
    if case == "disabled_plugin":
        p.client.settings.enabled = False
    if case == "closing":
        p._closing = True
    if case == "cooldown":
        p.client.settings.cooldown = 60
        p._cooldowns[("qq", "23456")] = time.monotonic()
    if case == "queue_full":
        p._pending = 1
    if case == "queue_timeout":
        await p._slots.acquire()
    if case == "operation_timeout":

        async def slow(*args):
            await asyncio.Event().wait()

        p._execute = slow
    if case == "send_false":
        p.ctx.send.image.return_value = False
    if case == "send_exception":
        p.ctx.send.image.side_effect = RuntimeError("private")
    if case == "error_send_exception":
        p.ctx.send.text.side_effect = RuntimeError("private")
    if case == "unexpected_handler_error":
        p._execute = AsyncMock(side_effect=RuntimeError("private"))
    dispatch = Dispatch(p)  # registration precedes runtime faults/replacement
    if case == "runner_exception":
        p.handle_command = AsyncMock(side_effect=RuntimeError("private"))
    if case == "runner_non_tuple":
        p.handle_command = AsyncMock(return_value=None)
    dispatch.rpc_failure = case == "rpc_exception"
    m = dispatch.message(text, media=media)
    if case == "group_denied":
        m.message_info.group_info.group_id = "other"
    if case == "private_denied":
        m.message_info.group_info = None
    if case == "wrong_platform":
        m.platform = "other"
    if case == "missing_user":
        m.message_info.user_info.user_id = ""
    try:
        await dispatch.bot.receive_message(m)
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        dispatch.bot._store_intercepted_command_message.assert_awaited_once_with(m)
        assert m.is_command
        assert m.message_info.additional_config["intercept_message_level"] == 1
        assert dispatch.registry.find_command_by_text(text) is not None
        assert dispatch.declaration["metadata"]["intercept_message_level"] == 1
        if case == "error_send_exception":
            assert p.ctx.send.text.await_count == 1  # no repeated error response attempt
        if case == "generate_image":
            assert dispatch.invocations[0]["args"]["message"]["raw_message"][1].get("binary_data_base64") is None
            p.ctx.send.image.assert_awaited_once()  # binary came from the official blocking Hook
    finally:
        if case == "queue_timeout":
            p._slots.release()
        await p.on_unload()


@pytest.mark.parametrize(
    "text",
    [
        "普通消息",
        "今天 /做表情 test",
        "做表情 test",
        "/做表情包 test",
        "/表情列表说明",
        "/表情帮助我",
        "/表情详情页",
        "/表情预览图",
        "https://example.invalid/做表情",
        "/other",
    ],
)
async def test_normal_messages_are_not_consumed(text):
    p = await plugin()
    dispatch = Dispatch(p)
    try:
        m = dispatch.message(text)
        await dispatch.bot.receive_message(m)
        assert not dispatch.invocations
        assert not m.is_command
        dispatch.bot._store_intercepted_command_message.assert_not_awaited()
        dispatch.bot.heartflow_message_receiver.process_message.assert_awaited_once_with(m)
    finally:
        await p.on_unload()


async def test_slow_generation_never_falls_through_while_awaiting():
    p = await plugin()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        entered.set()
        await release.wait()

    p._execute = slow
    dispatch = Dispatch(p)
    m = dispatch.message("/做表情 test hello")
    task = asyncio.create_task(dispatch.bot.receive_message(m))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert m.is_command  # set by Host before invoking the slow plugin
        assert not task.done()
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        release.set()
        await task
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        dispatch.bot._store_intercepted_command_message.assert_awaited_once_with(m)
    finally:
        release.set()
        await task
        await p.on_unload()


async def test_host_after_hook_can_explicitly_override_consumption():
    p = await plugin()
    dispatch = Dispatch(p)
    dispatch.after_override = {"continue_process": True, "intercept_message_level": 0}
    try:
        m = dispatch.message("/表情帮助")
        await dispatch.bot.receive_message(m)
        dispatch.bot.heartflow_message_receiver.process_message.assert_awaited_once_with(m)
    finally:
        await p.on_unload()
