# 开发与验证

## 运行后端回归测试

测试需要一份已安装依赖的 ComfyUI 0.45.0，使用其中的 Python 环境即可。所有测试文件及数据库均创建在临时目录；不会接触该实例的真实图片和运行历史。

```bash
COMFYUI_PATH=/path/to/ComfyUI /path/to/ComfyUI/.venv/bin/python -m unittest discover -s tests -v
```

覆盖单张与分组删除、部分失败、并发删除、缺失文件重试、同内容不同路径、input / output 区分、路径越界、视频附件、未启用资产数据库，以及 PNG、WebP EXIF、原生历史提示词和完整 seed。Qwen Image 2.1 的回归测试覆盖正负输出端口、空负向与缺少元数据的区分、反向连接和通过中间节点传递的文本链接。

使用 Node.js 运行前端文件定位测试：

```bash
node --test tests/descriptors.test.mjs
```

这三项测试确认视频预览缩略图、重命名资产的存储路径，以及原生预览框的目录类型不会改变删除目标。

## 前端适配依据

对应前端版本：1.58.5，提交 [`f7bcaf00095ddead1be4480b01f7cd6e851f726d`](https://github.com/Comfy-Org/ComfyUI_frontend/tree/f7bcaf00095ddead1be4480b01f7cd6e851f726d)。

- [AssetsSidebarTab.vue](https://github.com/Comfy-Org/ComfyUI_frontend/blob/f7bcaf00095ddead1be4480b01f7cd6e851f726d/src/components/sidebar/tabs/AssetsSidebarTab.vue)：原生菜单、分组卡片与大图入口。
- [MediaLightbox.vue](https://github.com/Comfy-Org/ComfyUI_frontend/blob/f7bcaf00095ddead1be4480b01f7cd6e851f726d/src/components/sidebar/tabs/queue/MediaLightbox.vue)：预览、索引更新与键盘导航。
- [useMediaAssetActions.ts](https://github.com/Comfy-Org/ComfyUI_frontend/blob/f7bcaf00095ddead1be4480b01f7cd6e851f726d/src/platform/assets/composables/useMediaAssetActions.ts)：本地版原生删除行为。
- [官方上下文菜单扩展说明](https://docs.comfy.org/custom-nodes/js/context-menu-migration)：公开扩展针对画布和节点，不包含资产库菜单。

组件定位、菜单回调和缓存输出过滤集中在 `web/native-assets.js`。它使用当前 Vue 组件树与 Pinia store，属于明确依赖前端版本的适配层。更新前端时应重新执行页面验收，不保留多版本兼容分支。

## 页面验收

使用单独的 ComfyUI 测试进程、临时 user / input / output 目录和数据库。通过 `--cpu` 启动，不需要加载模型。准备包含提示词元数据的测试图片，以及至少一个包含两张输出的原生历史任务。

验收以下行为：

1. 网格右键菜单出现唯一的删除操作，确认框标题为 ComfyUI；取消后文件和历史不变。
2. 双击大图，右侧显示真实元数据，所有提示词默认折叠。点击标题或用 Enter / 空格展开和折叠，展开后可复制；空负向显示“未填写”。切换图片同步更新并全部折叠，关闭后移除信息栏。
3. 在展开任务中删除一张图片，硬盘文件、历史输出和当前列表一起移除，另一张保留且可继续预览。
4. 删除最后一张后任务消失；切换网格 / 列表、退出再展开分组时不显示旧缓存图片。
5. 多选后分别验证右键删除和选择工具栏删除；分组卡片会处理整个任务。
6. Imported 页的删除只处理对应 input 文件，输出历史不受同名文件影响。
7. 新任务重新使用刚删除的文件名时，新的资产仍能正常显示。

本版本在 ComfyUI 0.45.0 / 前端 1.58.5 上完成上述页面验收，并分别检查了默认历史列表模式和 `--enable-assets` 模式。启用资产库的分组删除实测中，两张输出的文件及资产引用均清除，同内容的 input 参考图及其资产引用保留。后端 21 项回归测试和前端 3 项文件定位测试通过。

## 后端接口

`POST /better-assets/delete`

```json
{"files": [{"filename": "example.png", "subfolder": "album", "type": "output"}]}
```

分组卡片使用 `{"job_id": "..."}`，后端按原生历史和资产库解析具体输出。路径必须限制在配置的 input / output 根目录中。返回实际删除数、已处理文件、逐项错误，以及历史和资产清理计数；记录清理失败时返回 500，可以重试已不存在的文件。

`GET /better-assets/metadata?filename=example.png&subfolder=album&type=output`

返回文件信息、提取的提示词 / 模型 / 参数，以及原始 API prompt 和工作流文本。
