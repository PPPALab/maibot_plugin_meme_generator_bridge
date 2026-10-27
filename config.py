"""SDK configuration; safe defaults require an explicit deployment decision."""

from typing import List
from urllib.parse import urlsplit

from maibot_sdk import Field as SDKField, PluginConfigBase
from pydantic import field_validator


def Field(*args, description="", **kwargs):
    """Use the official SDK field helper, with Chinese labels in its supported UI metadata."""
    return SDKField(*args, description=description, json_schema_extra={"label": description or "设置"}, **kwargs)


class Settings(PluginConfigBase):
    __ui_label__ = "服务、访问权限与资源限制"
    __ui_order__ = 0

    config_version: str = Field(default="1.4.0", description="配置版本")
    enabled: bool = Field(default=False, description="配置服务后启用；群白名单为空时允许全部群")
    base_url: str = Field(default="", description="自部署 Rust 服务地址，不含 API 路径")
    allowed_groups: List[str] = Field(
        default_factory=list, description="QQ 群白名单；空表示允许全部群，非空仅允许所列群；私聊独立控制"
    )
    allow_private: bool = Field(default=False, description="允许 QQ 私聊")
    allow_quoted_images: bool = Field(
        default=True, description="允许将引用消息中的图片作为输入；关闭后只读取当前消息附图"
    )
    disabled_templates: List[str] = Field(default_factory=list, description="禁用模板 key 或精确关键词")
    timeout: float = Field(default=20, ge=1, le=120, description="单次网络请求超时秒数")
    operation_timeout: float = Field(default=45, ge=1, le=45, description="完整命令超时，需小于宿主 RPC 超时")
    concurrency: int = Field(default=2, ge=1, le=8, description="本插件同时执行命令/重请求数；不是 Rust 全局限额")
    recovery_epoch: int = Field(
        default=0, ge=0, description="重任务恢复序号；仅确认 Rust 已排空或重启后递增保存，普通保存不解除熔断"
    )
    max_waiters: int = Field(default=4, ge=0, le=32, description="最多排队命令数")
    queue_timeout: float = Field(default=3, ge=0.1, le=10, description="排队超时秒数")
    cooldown: float = Field(default=5, ge=0, le=3600, description="同一 QQ 用户命令冷却秒数")
    cache_ttl: float = Field(default=300, ge=1, le=86400, description="模板元数据缓存秒数")
    max_input_bytes: int = Field(default=4_000_000, ge=1024, le=8_000_000, description="每张输入图片上限")
    max_total_input_bytes: int = Field(
        default=8_000_000, ge=1024, le=32_000_000, description="一次制作所有图片原始字节总上限"
    )
    max_request_bytes: int = Field(
        default=6_000_000,
        ge=1024,
        le=16_000_000,
        description="每次 HTTP JSON 请求体上限；含 UTF-8、Base64 与 JSON 封装",
    )
    max_operation_request_bytes: int = Field(
        default=12_000_000, ge=1024, le=48_000_000, description="一次制作逐图上传与生成 JSON 请求体累计上限"
    )
    max_output_bytes: int = Field(
        default=6_000_000, ge=1024, le=12_000_000, description="成品图片上限，注意宿主 RPC 限制"
    )
    max_metadata_bytes: int = Field(default=4_000_000, ge=1024, le=16_000_000, description="元数据响应上限")
    max_images: int = Field(default=4, ge=1, le=8, description="输入图片数量上限")
    max_text_chars: int = Field(default=1000, ge=1, le=4000, description="每条文字字符上限")
    max_total_text_chars: int = Field(default=4000, ge=1, le=16000, description="一次制作全部文字字符总上限")
    max_total_text_bytes: int = Field(default=12000, ge=1, le=64000, description="一次制作全部文字 UTF-8 字节总上限")
    max_command_chars: int = Field(default=8000, ge=100, le=16000, description="命令字符上限")
    cache_cleanup_interval: float = Field(
        default=5, ge=1, le=30, description="主动清理过期头像/当前消息图片缓存的间隔秒数"
    )

    def allows_chat(self, group_id: str) -> bool:
        if group_id:
            return not self.allowed_groups or group_id in self.allowed_groups
        return self.allow_private

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("base_url 必须为无认证信息、无路径的 http(s) 服务根地址")
            _ = parsed.port
        return value


class MenuSettings(PluginConfigBase):
    __ui_label__ = "帮助与表情菜单"
    __ui_order__ = 1

    image_enabled: bool = Field(default=True, description="帮助/列表发送一张全量关键词索引图")
    text_fallback: bool = Field(default=True, description="索引图失败时发送一条文字版关键词清单")
    max_pixels: int = Field(default=4_000_000, ge=10000, le=8_000_000, description="索引图最大像素数")


class KeywordSettings(PluginConfigBase):
    __ui_label__ = "中文关键词触发"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="允许以真实模板关键词开头制作；关闭后仅支持斜杠命令")
    exclusions: List[str] = Field(default_factory=list, description="不接管的精确关键词；与其他插件冲突时填入此处")
    refresh_seconds: float = Field(
        default=300, ge=10, le=86400, description="模板关键词刷新间隔秒数；菜单查询也刷新快照"
    )


class AvatarSettings(PluginConfigBase):
    __ui_label__ = "真实 @ 头像获取"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="通过 SDK 获取真实 @ID 的头像地址，再受限下载官方头像")
    timeout: float = Field(default=8, ge=1, le=15, description="头像查询及下载总超时秒数")
    concurrency: int = Field(default=2, ge=1, le=4, description="头像下载最大并发数")
    cache_seconds: float = Field(default=300, ge=1, le=3600, description="头像内存缓存有效秒数")
    cache_entries: int = Field(default=16, ge=0, le=64, description="头像内存缓存条目数；0 表示关闭缓存")
    cache_bytes: int = Field(default=8_000_000, ge=1024, le=32_000_000, description="头像缓存总字节上限")


class BridgeConfig(PluginConfigBase):
    plugin: Settings = Field(default_factory=Settings)
    menu: MenuSettings = Field(default_factory=MenuSettings)
    keywords: KeywordSettings = Field(default_factory=KeywordSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
