# MaiBot 表情生成桥接

把自部署的 Rust 表情引擎 [meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs) 接入 MaiBot 的插件。

通过关键词、真实 @ 和附图触发表情生成，不靠 LLM 判断意图。

## 特性

- `/表情帮助` 发一张全量关键词索引图，`/表情帮助 关键词` 返回要求与用法示例
- 中文关键词直接触发，支持 @ 头像、当前消息附图与多段文字
- 官方设置页热更新，无需重启宿主
- 并发、排队、超时、冷却、输入/输出字节与像素均有限额
- GIF 成品按表情发送，静态图按图片发送
- 不使用外部图文渲染服务，不访问用户 URL

## 前置条件

- MaiBot **1.3.15** 与 `maibot-plugin-sdk` **2.16.0**（清单声明的兼容范围；其他版本请自行核对）
- 可用的 QQ 适配器插件（本插件不直接连接 QQ 协议，收发依赖宿主与适配器）
- 一个可访问的自部署 `meme-generator-rs` 服务

## 部署 Rust 服务

`deploy/compose.yaml` 提供了带资源限制的示例，配合 `deploy/.env` 使用：

```sh
cp deploy/.env.example deploy/.env
# 填入你本机核验过的镜像与端口
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

```dotenv
MEME_GENERATOR_IMAGE=ghcr.io/memecrafters/meme-generator-rs@sha256:<替换为你核验的 digest>
MEME_PORT=2233
```

- 示例镜像只作格式演示；请用 `docker buildx imagetools inspect` 等工具核验后固定 digest，不要长期依赖浮动标签。
- `/data` 卷用于持久化引擎数据；端口默认只绑定 `127.0.0.1`，需要外部访问时自行加反向代理与认证。
- MaiBot 也运行在 Docker 时，把两个容器接入同一用户网络，插件中的 `base_url` 用 `http://<服务名>:2233`，不要写 `localhost`。

## 安装插件

1. 将本目录放入 MaiBot 的 `plugins/` 下，例如 `plugins/maibot_plugin_meme_generator_bridge/`。
2. 依赖 `httpx`、`httpcore`、`Pillow` 由宿主插件依赖机制安装，SDK 由宿主提供。
3. 索引图使用系统本地中日韩字体（Noto Sans CJK、Unifont 等）；没有可用字体时会退回文字清单。

插件 ID 为 `github.pppalab.meme-generator-bridge`；仓库名称仍为 `PPPALab/maibot_plugin_meme_generator_bridge`。

## 配置

配置在 MaiBot **官方插件设置页**完成，保存后热更新（进行中任务会取消、连接重建、关键词快照刷新）。四个分组：

| 分组 | 字段 | 说明 |
| --- | --- | --- |
| plugin | `enabled` | 插件总开关 |
| plugin | `base_url` | 引擎根地址，不含 API 路径 |
| plugin | `allowed_groups` | 群白名单；**留空表示允许全部群**，非空仅允许所列群 |
| plugin | `allow_private` | 是否允许私聊，独立于群名单 |
| plugin | `allow_quoted_images` | 是否允许从当前会话的引用消息读取已入库图片 |
| plugin | `disabled_templates` | 禁用的模板 key 或精确关键词 |
| plugin | `timeout` / `operation_timeout` | 单次请求超时 / 完整命令超时 |
| plugin | `concurrency` / `max_waiters` | 同时执行数 / 最大排队数 |
| menu | `image_enabled` / `text_fallback` | 是否发索引图 / 失败时是否发一条文字清单 |
| keywords | `enabled` / `exclusions` / `refresh_seconds` | 关键词触发开关 / 冲突排除词 / 快照刷新间隔 |
| avatar | `enabled`、超时、并发、缓存 | 通过官方头像接口取真实 @ 头像 |

其余限额（输入输出字节、像素、文字长度、冷却、队列超时等）同样在设置页，默认值适配常见部署，按需收紧。

## 使用

```text
/表情帮助                 一张全量关键词索引图
/表情帮助 关键词           该模板所需图片/文字数量与用法示例
/表情列表                 同 /表情帮助
/表情详情 模板key          参数与说明
/表情预览 模板key          只生成预览图
/做表情 模板key "文字1"     生成并发送
```

关键词可以直接触发，不必写斜杠命令；引用一条图片消息后发送「锤」即用该图生成：

```text
关键词 @目标
关键词（当前消息附图）
关键词 "第一段文字" "含 空格的第二段"
@机器人 关键词
```

- 没有引用图片时，真实 @ 无论在关键词前后都按原顺序作为头像输入，包括 @机器人。
- 当前没有附图而引用了图片时，使用引用图，不再取 @ 头像；当前附图仍优先，不混入引用图。引用图不可用时明确报错，不偷偷改用头像。
- 附图必须附在当前消息里，数量与文字段数需满足模板要求；索引图中的长关键词会被省略，用 `/表情帮助 关键词` 查看完整信息。
- 未命中模板时只提示并给少量相近候选，不会替你选择或调用模型。

## 限制

- 支持引用一条图片消息后接关键词/命令（图片需仍在宿主图片库中）；仍不支持合并转发节点内图片与用户提供的图片 URL。
- 不支持模板的高级 `options` 自定义。
- @ 头像依赖适配器提供的官方头像接口，取不到时请直接附图。
- 上游 0.2.3 不支持 `server.body_limit` / `server.max_concurrent_tasks` 这两个可配置项，不能据此推断没有其他默认限制；部署时仍应设置插件限额与 Docker 资源限制。
- 多图模板的图片逐张上传，网络失败时不自动重试。
- 同一关键词对应多个模板时返回候选 key，不擅自选择。

## 排障

- **服务不可达**：检查 `base_url`、容器网络与引擎日志；MaiBot 在 Docker 内时不要用 `localhost`。
- **索引图失败**：会按开关发送一条文字关键词清单；多为字体缺失或像素上限过低。
- **模板缺失或被禁用**：以 `/表情帮助 关键词` 与引擎 `/meme/infos` 为准；检查 `disabled_templates`。
- **GIF 不动**：是否动态播放取决于宿主与 QQ 客户端。
- **发送失败**：插件不会自动重试，避免重复刷图；检查适配器回执与宿主 RPC 大小限制。
- **生成超时或繁忙**：降低并发、选轻量模板；被保护熔断时按设置页说明递增恢复序号。

## 隐私与数据

- 用于制图的图片与文字发送到你配置的 `base_url` 引擎；成品交由宿主和 QQ 适配器发送。插件不收集遥测。
- @ 头像功能通过宿主 `ctx.chat.get_avatar` 获取地址，再从受限的官方 QQ 头像 CDN 下载；不接受用户提供的任意 URL。
- 引擎自身会在临时目录保留素材，通常约 10–20 分钟后清理；持久化数据在 `/data` 卷。
- 插件的元数据、关键词与头像缓存都在内存中，条目数、字节数与有效期均有上限。

## 许可与致谢

本项目以 MIT 许可发布。上游 `meme-generator-rs` 亦为 MIT；表情模板与素材来自网络，不随本仓库分发，本项目不对素材授权作任何声明。
