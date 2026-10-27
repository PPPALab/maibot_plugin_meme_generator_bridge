"""User-visible keyword index/help, structured keyword admission and access semantics."""

from unittest.mock import AsyncMock
from io import BytesIO

import base64
import copy
import importlib

import pytest
from PIL import Image, ImageDraw

from host_dispatch import Dispatch
from test_bridge import GIF, PNG, ITEM, args, plugin, message
from test_features import HAMMER, finish_jobs, item

from maibot_plugin_meme_generator_bridge.client import BridgeError
from maibot_plugin_meme_generator_bridge.keywords import KeywordIndex
from maibot_plugin_meme_generator_bridge.menu import render_index


def many_items(count):
    return [item(f"k{i:03}", f"词{i}") for i in range(count)]


async def test_leading_real_at_is_consumed_by_official_host_contract():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    p.ctx.chat.get_avatar.return_value = {"status": "available", "url": "https://q1.qlogo.cn/g?b=qq&nk=34567&s=640"}
    p.avatars.get = AsyncMock(return_value=PNG)
    dispatch = Dispatch(p)
    m = dispatch.message(
        "",
        media=[
            {"type": "at", "data": {"target_user_id": "34567"}},
            {"type": "text", "data": " 锤"},
        ],
    )
    try:
        await dispatch.bot.receive_message(m)
        m.process.assert_not_awaited()
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        await finish_jobs(p)
        p.ctx.send.emoji.assert_awaited_once()
        assert p.client.generate.await_args.args[2] == [PNG]
    finally:
        await p.on_unload()


async def test_help_without_arguments_sends_one_index_image_for_300_templates():
    p = await plugin()
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(return_value=many_items(300))
    try:
        assert await p.handle_command(**args("/表情帮助")) == (True, None, 1)
        p.ctx.send.image.assert_awaited_once()
        p.ctx.send.text.assert_not_awaited()
        raw = p.ctx.send.image.await_args.args[0]
        with Image.open(BytesIO(base64.b64decode(raw))) as image:
            assert image.format == "PNG" and image.width == 1120
            assert image.width * image.height <= p.config.menu.max_pixels
    finally:
        await p.on_unload()


def test_index_draws_litteral_untrusted_keywords_with_ellipsis_and_column_budget(monkeypatch):
    labels = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *a, **kw):
        labels.append(text)
        return original(draw, xy, text, *a, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    evil = {**copy.deepcopy(ITEM), "key": "<script>x</script>", "keywords": ['<img src="https://bad.invalid/">']}
    long_word = "X" * 40
    png = render_index([evil, {**copy.deepcopy(ITEM), "keywords": [long_word]}], 39, 4_000_000, 6_000_000)
    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG" and image.width == 1120 and image.height == 112 + 34 + 46
    text = "\n".join(labels)
    assert "表情关键词索引" in text and "共 39 个" in text and "/表情帮助 关键词" in text
    assert "<img src=" in text and "…" in text  # literal drawing, bounded ellipsis
    # Pixel budget forces the column count up to its cap instead of failing.
    tight = render_index(many_items(400), 400, 1120 * (112 + 40 * 34 + 46), 6_000_000)
    with Image.open(BytesIO(tight)) as image:
        assert image.height == 112 + 40 * 34 + 46  # 10 columns


def test_index_rejects_empty_oversized_or_unrenderable_inputs():
    for items, pixels, size, font in [
        ([], 4_000_000, 6_000_000, None),
        (many_items(10_001), 4_000_000, 6_000_000, None),
        (many_items(400), 1120 * (112 + 39 * 34 + 46), 6_000_000, None),
        ([ITEM], 4_000_000, 10, None),
        ([ITEM], 4_000_000, 6_000_000, "/nonexistent/font.ttf"),
    ]:
        with pytest.raises(BridgeError):
            render_index(items, len(items), pixels, size, font_path=font)


async def test_index_failure_sends_one_truncated_text_fallback(monkeypatch):
    from maibot_plugin_meme_generator_bridge import plugin as plugin_module

    def fail(*a, **kw):
        raise BridgeError("绘制失败")

    monkeypatch.setattr(plugin_module, "render_index", fail)
    p = await plugin()
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(return_value=[item(f"k{i:03}", "超长关键词" * 200 + str(i)) for i in range(300)])
    try:
        assert await p.handle_command(**args("/表情帮助")) == (True, None, 1)
        p.ctx.send.image.assert_not_awaited()
        p.ctx.send.text.assert_awaited_once()
        text = p.ctx.send.text.await_args.args[0]
        assert "……" in text and len(text) <= 1600 and "共 300 个" in text and "绘制失败" in text
    finally:
        await p.on_unload()


async def test_index_send_false_keeps_single_attempt_and_reports(monkeypatch):
    from maibot_plugin_meme_generator_bridge import plugin as plugin_module

    monkeypatch.setattr(plugin_module, "render_index", lambda *a: PNG)
    p = await plugin()
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(return_value=many_items(300))
    p.ctx.send.image.return_value = False
    try:
        assert await p.handle_command(**args("/表情帮助")) == (True, None, 1)
        assert p.ctx.send.image.await_count == 1  # drawn once, never retried
        reply = p.ctx.send.text.await_args.args[0]
        assert "未确认发送" in reply and "词0" in reply
    finally:
        await p.on_unload()


async def test_help_with_keyword_returns_requirements_example_and_consumes():
    p = await plugin()
    p.client.metadata = AsyncMock(return_value=[HAMMER])
    p.client.generate = AsyncMock()
    try:
        for command in ["/表情帮助", "/表情列表"]:
            p.ctx.send.text.reset_mock()
            result = await p.handle_command(**args(f"{command} 锤"))
            assert result == (True, None, 1)
            p.client.generate.assert_not_awaited()
            text = p.ctx.send.text.await_args.args[0]
            assert "锤（hammer）" in text
            assert "图片 1–1 张" in text and "文字 0–0 段" in text
            assert "锤 @目标 [附图 1]" in text and "/表情预览 hammer" in text
    finally:
        await p.on_unload()


async def test_help_text_template_example_for_zero_image_template():
    p = await plugin()
    texts = {
        **copy.deepcopy(ITEM),
        "key": "words",
        "keywords": ["填词"],
        "params": {
            "min_images": 0,
            "max_images": 0,
            "min_texts": 1,
            "max_texts": 2,
            "default_texts": ["默认"],
            "options": [],
        },
    }
    p.client.resolve = AsyncMock(return_value=texts)
    try:
        assert await p.handle_command(**args("/表情帮助 填词")) == (True, None, 1)
        assert '填词 "文字"' in p.ctx.send.text.await_args.args[0]
    finally:
        await p.on_unload()


async def test_unknown_keyword_consumes_with_at_most_five_candidates_and_never_generates():
    p = await plugin()
    infos = [
        item("hammer", "锤"),
        item("hammering", "锤击"),
        item("wood", "木锤"),
        item("mallet", "锤子"),
        item("lighter", "打火机"),
        item("anvil", "铁砧"),
    ]
    p.client.metadata = AsyncMock(return_value=infos)
    p.client.generate = AsyncMock()
    try:
        result = await p.handle_command(**args("/表情帮助 机"))
        assert result == (False, None, 1)  # consumed, reported as an error reply
        p.client.generate.assert_not_awaited()
        text = p.ctx.send.text.await_args.args[0]
        assert "未找到该表情" in text
        candidates = text.split("相近候选：", 1)[1] if "相近候选：" in text else ""
        assert candidates and len(candidates.split("、")) <= 5 and "打火机" in candidates
        assert "自动" not in text
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ([{"type": "at", "data": {"target_user_id": "34567"}}], []),
        ([], [{"type": "at", "data": {"target_user_id": "34567"}}]),
        ([{"type": "image", "hash": "first"}], [{"type": "at", "data": {"target_user_id": "34567"}}]),
        ([{"type": "at", "data": {"target_user_id": "34567"}}, {"type": "image", "hash": "first"}], []),
    ],
)
def test_keyword_preserves_real_media_order_and_never_guesses_nickname(prefix, suffix):
    raw = [*prefix, {"type": "text", "data": '锤 "你好 世界"'}, *suffix]
    m = {**message(), "raw_message": raw}
    original = copy.deepcopy(m)
    result = KeywordIndex([HAMMER], []).match(m)
    assert result is not None and m == original
    assert [s for s in result[1]["raw_message"] if s["type"] != "text"] == prefix + suffix
    assert (
        KeywordIndex([HAMMER], []).match({**m, "raw_message": [*prefix, {"type": "text", "data": "普通聊天"}]}) is None
    )


def test_every_real_mention_is_preserved_regardless_of_bot_identity():
    target = {"type": "at", "data": {"target_user_id": "90000"}}
    index = KeywordIndex([HAMMER], [])
    raw = [target, {"type": "text", "data": "锤"}, target]
    for account in ["90000", "opaque-adapter", ""]:
        result = index.match({"raw_message": raw, "account_id": account})
        assert sum(s["type"] == "at" for s in result[1]["raw_message"]) == 2


async def test_bot_mention_plain_chat_passes_and_missing_input_is_still_consumed():
    p = await plugin(allowed_groups=[])
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(side_effect=BridgeError("图片数量要求 1–1，实际 0。"))
    dispatch = Dispatch(p)
    try:
        for keyword in [False, True]:
            m = dispatch.message(
                "",
                media=([] if keyword else [{"type": "at", "data": {"target_user_id": "90000"}}])
                + [{"type": "text", "data": "锤" if keyword else "普通聊天"}],
            )
            await dispatch.bot.receive_message(m)
            if not keyword:
                m.process.assert_awaited_once()
                dispatch.bot.heartflow_message_receiver.process_message.reset_mock()
                continue
            m.process.assert_not_awaited()
            dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
            await finish_jobs(p)
        p.ctx.chat.get_avatar.assert_not_awaited()
        assert p.client.generate.await_args.args[2] == []
        assert "图片数量" in p.ctx.send.text.await_args.args[0]
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "groups,group_id,private,enabled,allowed",
    [
        ([], "12345", False, True, True),
        (["12345"], "12345", False, True, True),
        (["other"], "12345", True, True, False),
        ([], "", False, True, False),
        (["other"], "", True, True, True),
        ([], "12345", True, False, False),
    ],
)
async def test_access_is_consistent_for_keyword_command_and_media_capture(groups, group_id, private, enabled, allowed):
    p = await plugin(allowed_groups=groups, allow_private=private)
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.config.plugin.enabled = enabled
    p._execute_keyword = AsyncMock()
    p._execute = AsyncMock()
    payload = {
        **message("锤"),
        "platform": "qq",
        "account_id": "90000",
        "message_info": {"group_info": {"group_id": group_id} if group_id else None, "user_info": {"user_id": "23456"}},
    }
    try:
        result = await p.handle_keyword(payload)
        assert result == ({"action": "abort"} if allowed else None)
        await finish_jobs(p)
        assert bool(p._execute_keyword.await_count) is allowed
        result = await p.handle_command(**{**args(), "group_id": group_id})
        assert result[0] is allowed and result[2] == 1
        media = {
            **payload,
            "raw_message": [
                {"type": "text", "data": "/做表情 锤"},
                {"type": "image", "hash": "h", "binary_data_base64": base64.b64encode(PNG).decode()},
            ],
        }
        await p.capture_current_media(message=media, command_name="meme_bridge")
        assert bool(p._media) is allowed
    finally:
        await p.on_unload()


async def test_empty_group_list_allows_help_but_private_stays_closed():
    p = await plugin(allowed_groups=[])
    try:
        result = await p.handle_command(**args("/表情帮助"))
        assert result == (True, None, 1)
        p.ctx.send.text.assert_awaited_once()
        p.ctx.send.text.reset_mock()
        assert await p.handle_command(**{**args("/表情帮助"), "group_id": ""}) == (False, None, 1)
        p.ctx.send.text.assert_not_awaited()
    finally:
        await p.on_unload()


def test_index_prefers_shortest_keyword_the_font_can_draw(monkeypatch):
    module = importlib.import_module("maibot_plugin_meme_generator_bridge.menu")
    monkeypatch.setattr(module, "_char_renderable", lambda path, size, char: char not in {"🤺", "🐔"})
    labels = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *a, **kw):
        labels.append(text)
        return original(draw, xy, text, *a, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    fencing = {**copy.deepcopy(ITEM), "key": "fencing", "keywords": ["击剑", "🤺"]}
    chicken = {**copy.deepcopy(ITEM), "key": "this_chicken", "keywords": ["这是鸡", "🐔"]}
    render_index([fencing, chicken], 2, 4_000_000, 6_000_000)
    text = "\n".join(labels)
    assert "击剑" in text and "这是鸡" in text
    assert "🤺" not in text and "🐔" not in text


def test_index_falls_back_to_template_key_when_no_keyword_renders(monkeypatch):
    module = importlib.import_module("maibot_plugin_meme_generator_bridge.menu")
    monkeypatch.setattr(module, "_char_renderable", lambda path, size, char: char != "🤺")
    labels = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *a, **kw):
        labels.append(text)
        return original(draw, xy, text, *a, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    emoji_only = {**copy.deepcopy(ITEM), "key": "fencing", "keywords": ["🤺"]}
    render_index([emoji_only], 1, 4_000_000, 6_000_000)
    assert "fencing" in labels and "🤺" not in labels


def test_index_selection_keeps_shortest_intent_for_normal_keywords(monkeypatch):
    module = importlib.import_module("maibot_plugin_meme_generator_bridge.menu")
    monkeypatch.setattr(module, "_char_renderable", lambda path, size, char: True)
    item_a = {**copy.deepcopy(ITEM), "key": "fencing", "keywords": ["击剑术", "击剑"]}
    item_b = {**copy.deepcopy(ITEM), "key": "ascii", "keywords": ["longer", "abc"]}
    labels = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *a, **kw):
        labels.append(text)
        return original(draw, xy, text, *a, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    render_index([item_a, item_b], 2, 4_000_000, 6_000_000)
    assert labels.count("击剑") == 1 and labels.count("abc") == 1


def test_real_font_reports_missing_emoji_glyph_but_keeps_ascii():
    module = importlib.import_module("maibot_plugin_meme_generator_bridge.menu")
    path = module.FONT_PATHS[0]  # DejaVu in this test process
    assert module._char_renderable(path, 20, "A") is True
    assert module._char_renderable(path, 20, "🤺") is False
    assert module._char_renderable(path, 20, "\ue000") is False
