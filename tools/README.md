# 重新生成随包菜单

仅开发时需要 Pillow 和本地字体；运行中的插件直接复用 `assets/` 中的 PNG。

在插件目录执行：

```sh
PYTHONPATH=.. python -m maibot_plugin_meme_generator_bridge.tools.build_menu \
  --metadata assets/menu-v0.2.3.json \
  --font /path/to/NotoSansSC-Regular.ttf
```

`--metadata` 也接受经核对的 v0.2.3 `/meme/infos` JSON 列表。清单记录模板 key、关键词、PNG 尺寸和 SHA256，以及制图字体的文件名、SHA256；复现时使用相同字体与 Pillow 版本。当前制图使用 Pillow 12.3.0。

脚本随源码分发，不进入运行用 wheel；字体文件不随包分发。更新模板后应重新生成 PNG 和 JSON，并检查图片及回归测试。
