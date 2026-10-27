"""Immutable exact-keyword snapshots; no regular expression supplied by a server."""

from typing import Any, Dict, List, Optional, Tuple

import shlex

from .inputs import segments


class KeywordIndex:
    def __init__(self, items: List[Dict[str, Any]], exclusions: List[str]):
        names = {
            name
            for item in items
            for name in item["keywords"]
            if name
            and len(name) <= 64
            and name == name.strip()
            and not name.startswith("/")
            and not any(char.isspace() for char in name)
            and name not in exclusions
        }
        self.names = tuple(sorted(names, key=lambda name: (-len(name), name)))

    def match(self, message: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        raw = segments(message)
        start = 0
        for part in raw:
            if part.get("type") in {"image", "emoji", "at", "reply"}:
                start += 1
            elif part.get("type") == "text" and isinstance(part.get("data"), str):
                if part["data"].strip():
                    break
                start += 1
            else:
                return None
        if start == len(raw):
            return None
        leading = 0
        texts = []
        for part in raw[start:]:
            if part.get("type") != "text" or not isinstance(part.get("data"), str):
                break
            leading += 1
            texts.append(part["data"])
        first = "".join(texts).lstrip()
        # Only structured mentions/media/whitespace may precede a literal keyword.
        for name in self.names:
            if not first.startswith(name):
                continue
            tail = first[len(name) :]
            if tail and not tail[0].isspace():
                continue
            # An exact keyword may touch a structured image/@, never a nickname string.
            end = start + leading
            if not tail and len(raw) > end and raw[end].get("type") not in {"image", "emoji", "at"}:
                continue
            # Preserve every actual @, including the bot, in its original order.
            # Image source precedence is decided by collect_images, not matching.
            prefix = [dict(item) for item in raw[:start]]
            command = {**raw[start], "data": "/做表情 " + shlex.quote(name) + tail}
            converted = [*prefix, command, *[dict(item) for item in raw[end:]]]
            return name, {**message, "raw_message": converted}
        return None
