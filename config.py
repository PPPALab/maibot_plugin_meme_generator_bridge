"""SDK configuration; human-readable UI units over exact internal byte limits."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, List, NamedTuple

from urllib.parse import urlsplit

from maibot_sdk import Field as SDKField, PluginConfigBase
from pydantic import field_validator, model_validator


BYTES_PER_MB = 1_000_000
BYTES_PER_KB = 1_000
PIXELS_PER_WAN = 10_000


def Field(*args, description="", label="", **kwargs):
    """Use the official SDK field helper, with short Chinese labels in UI metadata."""
    extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    extra.setdefault("label", label or description or "设置")
    if label:
        extra.setdefault("hint", description)
    return SDKField(*args, description=description, json_schema_extra=extra, **kwargs)


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是数字") from None
    if not result.is_finite():
        raise ValueError(f"{label}必须是有限数字")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_bytes(value: int) -> str:
    """Render bytes with decimal MB/KB, avoiding long bare integers."""
    value = int(value)
    if value >= BYTES_PER_MB:
        return f"{_decimal_text(Decimal(value) / Decimal(BYTES_PER_MB))} MB"
    if value >= BYTES_PER_KB:
        return f"{_decimal_text(Decimal(value) / Decimal(BYTES_PER_KB))} KB"
    return f"{value} 字节"


def format_pixels(value: int) -> str:
    """Render pixels as 万像素 when large enough."""
    value = int(value)
    if value >= PIXELS_PER_WAN:
        return f"{_decimal_text(Decimal(value) / Decimal(PIXELS_PER_WAN))} 万像素"
    return f"{value} 像素"


class _LimitSpec(NamedTuple):
    old_key: str
    new_key: str
    scale: int
    default: float
    minimum: int
    maximum: int
    label: str
    unit: str


_INPUT_MB = _LimitSpec("max_input_bytes", "max_input_mb", BYTES_PER_MB, 4.0, 1024, 8_000_000, "单图上限", "bytes")
_TOTAL_INPUT_MB = _LimitSpec(
    "max_total_input_bytes", "max_total_input_mb", BYTES_PER_MB, 8.0, 1024, 32_000_000, "全部输入图上限", "bytes"
)
_REQUEST_MB = _LimitSpec(
    "max_request_bytes", "max_request_mb", BYTES_PER_MB, 6.0, 1024, 16_000_000, "单次请求上限", "bytes"
)
_OPERATION_REQUEST_MB = _LimitSpec(
    "max_operation_request_bytes",
    "max_operation_request_mb",
    BYTES_PER_MB,
    12.0,
    1024,
    48_000_000,
    "整次累计请求上限",
    "bytes",
)
_OUTPUT_MB = _LimitSpec("max_output_bytes", "max_output_mb", BYTES_PER_MB, 6.0, 1024, 12_000_000, "成品上限", "bytes")
_METADATA_MB = _LimitSpec(
    "max_metadata_bytes", "max_metadata_mb", BYTES_PER_MB, 4.0, 1024, 16_000_000, "元数据上限", "bytes"
)
_TEXT_KB = _LimitSpec(
    "max_total_text_bytes", "max_total_text_kb", BYTES_PER_KB, 12.0, 1, 64_000, "文字总量上限", "bytes"
)
_PIXELS_WAN = _LimitSpec(
    "max_pixels", "max_pixels_wan", PIXELS_PER_WAN, 400.0, 10_000, 8_000_000, "索引图上限", "pixels"
)
_CACHE_MB = _LimitSpec("cache_bytes", "cache_mb", BYTES_PER_MB, 8.0, 1024, 32_000_000, "头像缓存上限", "bytes")

_SETTINGS_LIMITS = (_INPUT_MB, _TOTAL_INPUT_MB, _REQUEST_MB, _OPERATION_REQUEST_MB, _OUTPUT_MB, _METADATA_MB, _TEXT_KB)


def _display_limit(spec: _LimitSpec, value: int) -> str:
    return format_pixels(value) if spec.unit == "pixels" else format_bytes(value)


def _validate_unit(value: Any, spec: _LimitSpec) -> float:
    decimal = _decimal(value, spec.label)
    # Bounds before conversion also avoid allocating giant integers for huge input.
    if decimal < Decimal(spec.minimum) / spec.scale or decimal > Decimal(spec.maximum) / spec.scale:
        raise ValueError(
            f"{spec.label}必须在 {_display_limit(spec, spec.minimum)}–{_display_limit(spec, spec.maximum)} 之间"
        )
    # Integer ratios are exact, including decimal tails longer than Decimal's context.
    numerator, denominator = decimal.as_integer_ratio()
    _, remainder = divmod(numerator * spec.scale, denominator)
    unit = "像素" if spec.unit == "pixels" else "字节"
    if remainder:
        raise ValueError(f"{spec.label}必须精确到 1 {unit}，不能用当前小数")
    result = float(decimal)
    if Decimal(str(result)) != decimal:
        raise ValueError(f"{spec.label}的小数无法无损保存，请精确到 1 {unit}")
    return result


def _validate_legacy_int(value: Any, spec: _LimitSpec) -> int:
    unit = "像素" if spec.unit == "pixels" else "bytes"
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"旧配置 {spec.old_key} 必须为整数 {unit}")
    if not spec.minimum <= value <= spec.maximum:
        raise ValueError(
            f"{spec.label}必须在 {_display_limit(spec, spec.minimum)}–{_display_limit(spec, spec.maximum)} 之间"
        )
    return value


def _bytes_to_unit(value: int, scale: int) -> float:
    return float(Decimal(value) / Decimal(scale))


def _unit_to_bytes(value: float, scale: int) -> int:
    return int(Decimal(str(value)) * Decimal(scale))


def _is_default(value: Any, default: float) -> bool:
    try:
        return _decimal(value, "配置") == Decimal(str(default))
    except ValueError:
        return False


def _migrate_legacy_limits(data: Any, specs: tuple[_LimitSpec, ...]) -> Any:
    """Migrate old byte/pixel fields before validation; new non-default value wins."""
    if not isinstance(data, Mapping):
        return data
    migrated = dict(data)
    for spec in specs:
        if spec.old_key not in migrated:
            continue
        value = migrated.pop(spec.old_key)
        if value is None:  # Host upgrade skeleton: absent legacy value, not a limit.
            continue
        legacy = _validate_legacy_int(value, spec)
        if spec.new_key in migrated and not _is_default(migrated[spec.new_key], spec.default):
            continue
        migrated[spec.new_key] = _bytes_to_unit(legacy, spec.scale)
    return migrated


def _legacy_defaults(specs: tuple[_LimitSpec, ...]) -> dict[str, Any]:
    # Host's type-filtered rebuild preserves every value when the skeleton is None.
    # This lets our validator reject malformed old values rather than silently reset.
    return {spec.old_key: None for spec in specs}


LEGACY_LIMIT_DEFAULTS = {
    "plugin": _legacy_defaults(_SETTINGS_LIMITS),
    "menu": _legacy_defaults((_PIXELS_WAN,)),
    "avatar": _legacy_defaults((_CACHE_MB,)),
}


class Settings(PluginConfigBase):
    __ui_label__ = "服务、访问权限与资源限制"
    __ui_order__ = 0

    config_version: str = Field(default="1.5.0", description="配置版本")
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
    max_input_mb: float = Field(
        default=4.0,
        ge=0.001024,
        le=8.0,
        label="单图上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="每张输入图片原始字节；1 MB=1,000,000 bytes；Base64 与 JSON 会放大请求体积",
    )
    max_total_input_mb: float = Field(
        default=8.0,
        ge=0.001024,
        le=32.0,
        label="全部输入图上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="一次制作所有输入图片原始字节合计；1 MB=1,000,000 bytes",
    )
    max_request_mb: float = Field(
        default=6.0,
        ge=0.001024,
        le=16.0,
        label="单次请求上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="每次 HTTP JSON 请求体上限（含 UTF-8、Base64 与 JSON 封装）；1 MB=1,000,000 bytes",
    )
    max_operation_request_mb: float = Field(
        default=12.0,
        ge=0.001024,
        le=48.0,
        label="整次累计请求上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="一次制作逐图上传与生成 JSON 请求体累计上限；1 MB=1,000,000 bytes",
    )
    max_output_mb: float = Field(
        default=6.0,
        ge=0.001024,
        le=12.0,
        label="成品上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="成品图片字节上限；1 MB=1,000,000 bytes；注意宿主 RPC 限制",
    )
    max_metadata_mb: float = Field(
        default=4.0,
        ge=0.001024,
        le=16.0,
        label="元数据上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="元数据响应上限；1 MB=1,000,000 bytes",
    )
    max_images: int = Field(default=4, ge=1, le=8, description="输入图片数量上限")
    max_text_chars: int = Field(default=1000, ge=1, le=4000, description="每条文字字符上限")
    max_total_text_chars: int = Field(default=4000, ge=1, le=16000, description="一次制作全部文字字符上限")
    max_total_text_kb: float = Field(
        default=12.0,
        ge=0.001,
        le=64.0,
        label="文字总量上限 (KB)",
        json_schema_extra={"step": 0.001},
        description="一次制作全部文字 UTF-8 字节合计；1 KB=1,000 bytes",
    )
    max_command_chars: int = Field(default=8000, ge=100, le=16000, description="命令字符上限")
    cache_cleanup_interval: float = Field(
        default=5, ge=1, le=30, description="主动清理过期头像/当前消息图片缓存的间隔秒数"
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_limits(cls, data: Any) -> Any:
        migrated = _migrate_legacy_limits(data, _SETTINGS_LIMITS)
        if isinstance(migrated, dict):
            version = str(migrated.get("config_version", "1.5.0")).split(".")
            if len(version) == 3 and all(part.isdigit() for part in version):
                if tuple(map(int, version)) < (1, 5, 0):
                    migrated["config_version"] = "1.5.0"
        return migrated

    @field_validator("max_input_mb", mode="before")
    @classmethod
    def _validate_max_input_mb(cls, value: Any) -> float:
        return _validate_unit(value, _INPUT_MB)

    @field_validator("max_total_input_mb", mode="before")
    @classmethod
    def _validate_max_total_input_mb(cls, value: Any) -> float:
        return _validate_unit(value, _TOTAL_INPUT_MB)

    @field_validator("max_request_mb", mode="before")
    @classmethod
    def _validate_max_request_mb(cls, value: Any) -> float:
        return _validate_unit(value, _REQUEST_MB)

    @field_validator("max_operation_request_mb", mode="before")
    @classmethod
    def _validate_max_operation_request_mb(cls, value: Any) -> float:
        return _validate_unit(value, _OPERATION_REQUEST_MB)

    @field_validator("max_output_mb", mode="before")
    @classmethod
    def _validate_max_output_mb(cls, value: Any) -> float:
        return _validate_unit(value, _OUTPUT_MB)

    @field_validator("max_metadata_mb", mode="before")
    @classmethod
    def _validate_max_metadata_mb(cls, value: Any) -> float:
        return _validate_unit(value, _METADATA_MB)

    @field_validator("max_total_text_kb", mode="before")
    @classmethod
    def _validate_max_total_text_kb(cls, value: Any) -> float:
        return _validate_unit(value, _TEXT_KB)

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

    @property
    def max_input_bytes(self) -> int:
        return _unit_to_bytes(self.max_input_mb, _INPUT_MB.scale)

    @max_input_bytes.setter
    def max_input_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _INPUT_MB)
        self.max_input_mb = _bytes_to_unit(value, _INPUT_MB.scale)

    @property
    def max_total_input_bytes(self) -> int:
        return _unit_to_bytes(self.max_total_input_mb, _TOTAL_INPUT_MB.scale)

    @max_total_input_bytes.setter
    def max_total_input_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _TOTAL_INPUT_MB)
        self.max_total_input_mb = _bytes_to_unit(value, _TOTAL_INPUT_MB.scale)

    @property
    def max_request_bytes(self) -> int:
        return _unit_to_bytes(self.max_request_mb, _REQUEST_MB.scale)

    @max_request_bytes.setter
    def max_request_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _REQUEST_MB)
        self.max_request_mb = _bytes_to_unit(value, _REQUEST_MB.scale)

    @property
    def max_operation_request_bytes(self) -> int:
        return _unit_to_bytes(self.max_operation_request_mb, _OPERATION_REQUEST_MB.scale)

    @max_operation_request_bytes.setter
    def max_operation_request_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _OPERATION_REQUEST_MB)
        self.max_operation_request_mb = _bytes_to_unit(value, _OPERATION_REQUEST_MB.scale)

    @property
    def max_output_bytes(self) -> int:
        return _unit_to_bytes(self.max_output_mb, _OUTPUT_MB.scale)

    @max_output_bytes.setter
    def max_output_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _OUTPUT_MB)
        self.max_output_mb = _bytes_to_unit(value, _OUTPUT_MB.scale)

    @property
    def max_metadata_bytes(self) -> int:
        return _unit_to_bytes(self.max_metadata_mb, _METADATA_MB.scale)

    @max_metadata_bytes.setter
    def max_metadata_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _METADATA_MB)
        self.max_metadata_mb = _bytes_to_unit(value, _METADATA_MB.scale)

    @property
    def max_total_text_bytes(self) -> int:
        return _unit_to_bytes(self.max_total_text_kb, _TEXT_KB.scale)

    @max_total_text_bytes.setter
    def max_total_text_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _TEXT_KB)
        self.max_total_text_kb = _bytes_to_unit(value, _TEXT_KB.scale)


class MenuSettings(PluginConfigBase):
    __ui_label__ = "帮助与表情菜单"
    __ui_order__ = 1

    image_enabled: bool = Field(default=True, description="帮助/列表发送一张全量关键词索引图")
    text_fallback: bool = Field(default=True, description="索引图失败时发送一条文字版关键词清单")
    max_pixels_wan: float = Field(
        default=400.0,
        ge=1.0,
        le=800.0,
        label="索引图上限 (万像素)",
        json_schema_extra={"step": 0.0001},
        description="索引图像素上限；1 万=10,000 像素；同时受输出字节限制",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_max_pixels(cls, data: Any) -> Any:
        return _migrate_legacy_limits(data, (_PIXELS_WAN,))

    @field_validator("max_pixels_wan", mode="before")
    @classmethod
    def _validate_max_pixels_wan(cls, value: Any) -> float:
        return _validate_unit(value, _PIXELS_WAN)

    @property
    def max_pixels(self) -> int:
        return _unit_to_bytes(self.max_pixels_wan, _PIXELS_WAN.scale)

    @max_pixels.setter
    def max_pixels(self, value: int) -> None:
        _validate_legacy_int(value, _PIXELS_WAN)
        self.max_pixels_wan = _bytes_to_unit(value, _PIXELS_WAN.scale)


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
    cache_mb: float = Field(
        default=8.0,
        ge=0.001024,
        le=32.0,
        label="头像缓存上限 (MB)",
        json_schema_extra={"step": 0.000001},
        description="头像内存缓存总字节；1 MB=1,000,000 bytes",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_cache_bytes(cls, data: Any) -> Any:
        return _migrate_legacy_limits(data, (_CACHE_MB,))

    @field_validator("cache_mb", mode="before")
    @classmethod
    def _validate_cache_mb(cls, value: Any) -> float:
        return _validate_unit(value, _CACHE_MB)

    @property
    def cache_bytes(self) -> int:
        return _unit_to_bytes(self.cache_mb, _CACHE_MB.scale)

    @cache_bytes.setter
    def cache_bytes(self, value: int) -> None:
        _validate_legacy_int(value, _CACHE_MB)
        self.cache_mb = _bytes_to_unit(value, _CACHE_MB.scale)


class BridgeConfig(PluginConfigBase):
    plugin: Settings = Field(default_factory=Settings)
    menu: MenuSettings = Field(default_factory=MenuSettings)
    keywords: KeywordSettings = Field(default_factory=KeywordSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
