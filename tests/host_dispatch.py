"""Execute inspected Host method bodies, with IO/RPC/session/Hook boundaries replaced.

No src imports, host startup, databases or LLM calls. This is a dispatch contract
test, not a running MaiBot integration. Sources are read-only and never vendored.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Tuple
from unittest.mock import AsyncMock

import ast
import copy
import logging
import os
import re
import traceback

import pytest


def host_root():
    root = Path(__file__).resolve().parents[1]
    host = Path(os.environ.get("MAIBOT_REFERENCE", root.parent / "reference" / "MaiBot"))
    if not (host / "src/chat/message_receive/bot.py").is_file():
        pytest.skip("MAIBOT_REFERENCE required for actual host method contract tests")
    return host


def methods(source, class_name, names, namespace):
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    selected = [
        node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in selected} == set(names)
    cls = ast.ClassDef(name=class_name, bases=[], keywords=[], body=selected, decorator_list=[])
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<host-contract>", "exec"),
        namespace,
    )
    return namespace[class_name]


class Payload(SimpleNamespace):
    @classmethod
    def model_validate(cls, value):
        return cls(**value)

    def model_dump(self):
        return vars(self)


class Envelope(SimpleNamespace):
    def make_response(self, payload):
        return SimpleNamespace(payload=payload)


class CommandEntry(SimpleNamespace):
    pass


class Dispatch:
    def __init__(self, plugin):
        self.plugin = plugin
        self.after_override = None
        self.rpc_failure = False
        self.invocations = []
        host = host_root()
        self.declaration = next(c for c in plugin.get_components() if c["type"] == "COMMAND")
        metadata = self.declaration["metadata"]
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "Optional": Optional,
            "Callable": Callable,
            "Tuple": Tuple,
            "CommandExecutor": Any,
            "SessionMessage": Any,
            "Envelope": Envelope,
            "InvokePayload": Payload,
            "InvokeResultPayload": Payload,
            "datetime": datetime,
            "traceback": traceback,
            "logger": logging.getLogger("dispatch-contract"),
            "is_local_operator": lambda *a: False,
            "is_clear_context_command": lambda *a: False,
            "is_command_disabled": lambda *a: False,
            "global_announcement_manager": SimpleNamespace(get_disabled_chat_commands=lambda *a: []),
            "global_config": SimpleNamespace(voice=SimpleNamespace(enable_asr=False)),
            "RouteKeyFactory": SimpleNamespace(
                from_session_message=lambda m: SimpleNamespace(account_id="90000", scope="route")
            ),
            "SessionUtils": SimpleNamespace(calculate_session_id=lambda *a, **kw: "stream"),
            "process_received_images_in_message": lambda c: SimpleNamespace(compressed_count=0, discarded_count=0),
            "MessageUtils": SimpleNamespace(
                check_ban_words=lambda t: (False, None), check_ban_regex=lambda t: (False, None)
            ),
            "chat_manager": SimpleNamespace(register_message=lambda m: None, get_or_create_session=AsyncMock()),
            "resolve_component_rpc_timeout_ms": lambda v: v or 60000,
            "PluginMessageUtils": SimpleNamespace(_session_message_to_dict=self.serialize),
            "ComponentTypes": SimpleNamespace(COMMAND="COMMAND"),
            "CommandEntry": CommandEntry,
            "ComponentEntry": Any,
        }
        registry_class = methods(
            (host / "src/plugin_runtime/host/component_registry.py").read_text(),
            "ComponentRegistry",
            ["find_command_by_text"],
            namespace,
        )
        registry = registry_class()
        entry = CommandEntry(compiled_pattern=re.compile(metadata["command_pattern"]), aliases=metadata["aliases"])
        registry._by_type = {"COMMAND": {"meme_bridge": entry}}
        registry.check_component_enabled = lambda *a: True
        self.registry = registry

        runner_class = methods(
            (host / "src/plugin_runtime/runner/runner_main.py").read_text(),
            "PluginRunner",
            ["_handle_invoke", "_handle_hook_invoke"],
            namespace,
        )
        runner = runner_class()
        runner._loader = SimpleNamespace(get_plugin=lambda id: SimpleNamespace(config_error=None, instance=plugin))
        runner._resolve_component_handler = lambda meta, name: (
            plugin.handle_keyword if name == "meme_keyword_trigger" else plugin.handle_command
        )
        runner._start_inflight_rpc = lambda *a: None
        runner._finish_inflight_rpc = lambda *a: None

        async def invoke_callable(handler, **kw):
            return await handler(**kw)

        runner._invoke_plugin_callable = invoke_callable

        async def invoke_plugin(**kw):
            if self.rpc_failure:
                raise RuntimeError("RPC unavailable")
            self.invocations.append(kw)
            return await runner._handle_invoke(
                Envelope(
                    plugin_id="local.meme-generator-bridge",
                    payload={"component_name": kw["component_name"], "args": kw["args"]},
                )
            )

        executor_class = methods(
            (host / "src/plugin_runtime/component_query.py").read_text(),
            "ComponentQueryService",
            ["_build_command_executor"],
            namespace,
        )
        executor = executor_class._build_command_executor(
            SimpleNamespace(invoke_plugin=invoke_plugin),
            "local.meme-generator-bridge",
            "meme_bridge",
            metadata,
        )
        info = SimpleNamespace(plugin_name="local.meme-generator-bridge", name="meme_bridge", permission="user")

        def find_command(text):
            result = registry.find_command_by_text(text)
            return (executor, result[1], info) if result else None

        namespace["component_query_service"] = SimpleNamespace(
            find_command_by_text=find_command, get_plugin_config=lambda id: {}
        )
        bot_class = methods(
            (host / "src/chat/message_receive/bot.py").read_text(),
            "ChatBot",
            [
                "_coerce_int",
                "_mark_command_message",
                "_is_command_candidate",
                "_process_commands",
                "_handle_command_processing_result",
                "receive_message",
            ],
            namespace,
        )
        self.bot = bot_class()
        self.bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        self.bot._store_intercepted_command_message = AsyncMock()
        self.bot._process_adapter_lifecycle_command = AsyncMock(return_value=False)
        self.bot._process_clear_context_command = AsyncMock(return_value=False)
        self.bot.handle_notice_message = AsyncMock()

        async def hook(name, message, **kwargs):
            if name == "chat.receive.before_process":
                response = await runner._handle_hook_invoke(
                    Envelope(
                        plugin_id="local.meme-generator-bridge",
                        payload={
                            "component_name": "meme_keyword_trigger",
                            "args": {"message": self.serialize(message)},
                        },
                    )
                )
                result = response.payload
                if result.get("success") and result.get("action") == "abort":
                    return SimpleNamespace(aborted=True, kwargs=kwargs), message
            if name == "chat.command.before_execute":
                await plugin.capture_current_media(message=self.serialize(message), **kwargs)
            if name == "chat.command.after_execute" and self.after_override is not None:
                kwargs.update(self.after_override)
            return SimpleNamespace(aborted=False, kwargs=kwargs), message

        self.bot._invoke_message_hook = hook

    @staticmethod
    def message(text, group_id="12345", user_id="23456", platform="qq", media=None):
        info = SimpleNamespace(
            group_info=SimpleNamespace(group_id=group_id, group_name="test") if group_id else None,
            user_info=SimpleNamespace(user_id=user_id),
            additional_config={},
        )
        return SimpleNamespace(
            received_at=None,
            message_id="msg",
            session_id="stream",
            platform=platform,
            message_info=info,
            account_id="90000",
            scope="route",
            processed_plain_text=text,
            is_command=False,
            raw_message=SimpleNamespace(components=[]),
            process=AsyncMock(),
            raw_segments=[{"type": "text", "data": text}, *(media or [])],
        )

    @staticmethod
    def serialize(message, include_binary_data=True):
        raw = copy.deepcopy(message.raw_segments)
        if not include_binary_data:
            for item in raw:
                item.pop("binary_data_base64", None)
        info = message.message_info
        return {
            "message_id": message.message_id,
            "session_id": message.session_id,
            "platform": message.platform,
            "account_id": message.account_id,
            "scope": message.scope,
            "raw_message": raw,
            "message_info": {
                "group_info": {"group_id": info.group_info.group_id} if info.group_info else None,
                "user_info": {"user_id": info.user_info.user_id},
            },
        }
