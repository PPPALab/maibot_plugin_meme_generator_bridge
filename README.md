# 表情包制作桥接器

把自部署的 Rust 表情引擎 [meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs) 接入 MaiBot。用中文关键词或斜杠命令直接生成表情，不依赖 LLM 判断意图。

## 特性

- 关键词直接触发，支持真实 @、当前附图、同一会话引用图和多段文字
- `/表情帮助` 发关键词索引图；支持详情、预览和制作命令
- 官方设置页配置，保存后热更新，无需重启宿主
- GIF 成品按表情发送，静态图按图片发送
- 超时、并发、排队、冷却和输入/输出大小均有限额

## 前置条件

- MaiBot **1.3.19** 与 `maibot-plugin-sdk` **2.20.0**（本次明确验证的版本组合；旧版请使用对应历史发布）
- 可用的 QQ 适配器插件
- 一个可访问的自部署 `meme-generator-rs` 服务

## 部署 Rust 服务

`deploy/compose.yaml` 提供了带资源限制的示例：

```sh
cp deploy/.env.example deploy/.env
# 填入核验过的镜像 digest 与端口
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

```dotenv
MEME_GENERATOR_IMAGE=ghcr.io/memecrafters/meme-generator-rs@sha256:<核验后的 digest>
MEME_PORT=2233
```

- 示例镜像只作格式演示；请核验后固定 digest。
- 数据保存在 Docker 命名卷 `meme-data`，不会在当前目录生成 `data/`；`docker compose down -v` 会删除。
- 端口默认只绑定 `127.0.0.1`。MaiBot 也在 Docker 时，把两个容器接入同一网络，`base_url` 用 `http://<服务名>:2233`，不要写 `localhost`。

## 安装插件

把插件目录放入 MaiBot 的 `plugins/` 下，例如 `plugins/maibot_plugin_meme_generator_bridge/`。

1. 依赖 `httpx`、`httpcore`、`Pillow` 由宿主自动安装，SDK 由宿主提供。
2. 索引图需要系统本地中日韩字体（Noto Sans CJK、Unifont 等）；缺失时按开关退回文字清单。
3. 在官方设置页填写 `base_url` 并启用。`enabled` 默认关闭；`base_url` 是 meme-generator-rs 的根地址，不是 MaiBot 地址。

插件 ID：`github.pppalab.meme-generator-bridge`。

## 配置

在 MaiBot **官方插件设置页**完成，保存后热更新。

- 服务：`enabled`（默认关闭）、`base_url`（Rust 根地址，不含路径、认证或查询参数）
- 权限：`allowed_groups`（留空允许全部群）、`allow_private`、`allow_quoted_images`
- 模板与触发：`disabled_templates`、`keywords.enabled`、`keywords.exclusions`
- 限额：`timeout`、`operation_timeout`、`concurrency`、`max_waiters`、`cooldown`
- 菜单与头像：`menu.image_enabled`、`menu.text_fallback`、`avatar.enabled` 及缓存

其余输入/输出大小、图片数量和文字长度等限额也在设置页；大小按十进制 MB / KB（1 MB=1,000,000 字节，1 KB=1,000 字节）填写，索引图按万像素填写，均支持小数。旧配置会自动换算，实际限额不变。

## 使用

```text
/表情帮助 [关键词]              无参数发索引图；带关键词查看参数与用法
/表情列表 [关键词]              同 /表情帮助
/表情详情 <模板 key 或关键词>    参数与说明
/表情预览 <模板 key 或关键词>    只生成预览图
/做表情 <模板 key 或关键词> "文字1" "文字2"
```

关键词可以直接触发：

```text
关键词 @目标
关键词（当前消息附图）
关键词 "第一段文字" "含 空格的第二段"
@机器人 关键词
例 锤@x
```

图片与 @ 的取值顺序：

1. 当前消息有图片/表情：使用当前图片和所有 @，按消息顺序；引用图不参与。
2. 当前消息没有图片、引用了同一会话消息：使用引用图，@ 不参与（需开启 `allow_quoted_images`）。
3. 两者都没有：使用所有 @ 头像。

- @ 机器人也算 @，前后位置都支持；只认结构化 @ID，不支持 @全体成员或按昵称猜测。
- 当前附图与引用图不混用；引用消息无可用图片会报错，不会改用 @ 头像。
- 图片总数受 `max_images` 限制（含 @ 头像）；文字必须显式给出。
- 引用图需仍在宿主图片库；机器人发出但未入库的表情可能不可用。
- 索引图每个模板只列一个最短关键词，长词用 `/表情帮助 关键词` 查看。

## 限制

- 关键词命中后，消息会在 `chat.receive.before_process` 被中止，因此不会进入宿主聊天记录，也不会经过宿主禁用词过滤。这是确定性命中机制的取舍；可用 `keywords.enabled` 关闭关键词触发，或用 `keywords.exclusions` 排除特定词。
- 宿主的前置媒体处理、其他插件和已排队的聊天任务仍可能运行；本插件不承诺整条聊天链路零模型调用。
- 不支持合并转发节点内的图片、用户提供的图片 URL 和模板高级 `options` 自定义。
- @ 头像依赖适配器提供的官方头像接口，取不到时请直接附图。
- 上游 0.2.3 不支持 `server.body_limit` / `server.max_concurrent_tasks` 这两个可配置项，不能据此推断没有其他默认限制。
- 多图模板逐张上传，失败不自动重试；同一关键词对应多个模板时返回候选 key，不擅自选择。

## 排障

- **服务不可达**：检查 `base_url`、容器网络和引擎日志；Docker 内不要写 `localhost`。
- **引用图不可用**：图片可能未入库、已过期、来自其他会话，或是机器人发出但未入库的表情；重新附图即可。
- **索引图失败**：会按开关发送文字清单；多为字体缺失或像素上限过低。
- **生成超时或繁忙**：降低并发、选轻量模板；熔断后按设置页说明递增恢复序号。

## 隐私与数据

- 制图用的图片和文字会发送到配置的 `base_url` 引擎；成品交由 MaiBot 和 QQ 适配器发送。插件不收集遥测。
- 引用图通过 MaiBot 宿主 `message.get_by_id` 读取，仅限当前会话；@ 头像通过宿主 `get_avatar` 获取地址后，从受限的 QQ 官方头像 CDN 下载。
- 引擎临时素材的清理策略以上游版本为准；插件缓存仅在内存中有界保存。

## 许可与致谢

本项目以 MIT 许可发布。表情模板与素材来自网络，不随本仓库分发。

- [MemeCrafters/meme-generator](https://github.com/MemeCrafters/meme-generator)：Python 原版（MIT）。
- [MemeCrafters/meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs)：本插件对接的 Rust 版（MIT）。
