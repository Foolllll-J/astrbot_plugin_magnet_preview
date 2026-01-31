<div align="center">

# 🧲 磁链预览助手

<i>🔍 一键解析磁链，资源尽在掌握</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

---

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的轻量级磁链预览插件，调用 [whatslink](https://whatslink.info) API 实现。支持对磁力链接（Magnet）及 40 位 InfoHash 的自动解析与指令查询，能够快速提取并展示资源名称、大小、文件清单及预览截图。

---

## ✨ 功能特性

* **🚀 自动触发**: 智能检测消息中的磁力链接或 40 位 InfoHash 并自动解析预览。
* **🔢 多链支持**: 支持单条消息内解析多个磁链，解析结果通过合并转发节点清晰展示。
- **⏳ 状态反馈**: 识别到磁链并开始解析时，会自动给消息贴上表情，便于判断解析结果是否被吞。
* **💬 指令解析**: 支持 `/磁链` 指令，可直接输入磁链或通过引用普通消息、**合并转发聊天记录**解析其中的内容。
* **📸 丰富展示**: 详细展示文件名、大小、文件清单及预览截图，支持图片模糊打码。
* **⚙️ 灵活配置**: 支持白名单管理、截图数量控制、模糊程度调节及直链/图片模式切换。

---

## 📖 使用指南

### 📝 自动解析

在聊天中直接发送包含磁链的消息，插件将自动提取并回复。

* **示例**: `magnet:?xt=urn:btih:C14ED88B609F9C57A39067C51E80024AB7DCXXXX` 或直接发送 `40位哈希值`。

### 🔍 指令解析

使用指令手动触发解析，支持别名 `磁力`。

| 指令 | 说明 |
| :--- | :--- |
| `/磁链 [磁链/哈希]` | 直接解析提供的磁链或哈希值。 |
| `引用消息 + /磁链` | 解析被引用消息（支持文字消息、**合并转发记录**）中的磁链。 |
| `/磁链 [索引] [模糊度]` | 解析被引用消息中的第 N 个磁链，并指定模糊度（0-10）。例如 `/磁链 2 3`。 |
| `/磁链 [模糊度]` | 当引用消息只有一条磁链时，指定预览图模糊度（0-10）。例如 `/磁链 5`。 |

> **提示**: 若提供了模糊度参数，将无视 `output_as_link` 配置，强制发送预览图。

---

## ⚙️ 配置选项

可在 AstrBot 管理面板或 `_conf_schema.json` 中调整以下参数：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `group_whitelist` | `[]` | 允许运行的群组白名单。**私聊场景默认不受限**。留空则全局启用。 |
| `auto_parse` | `true` | 是否自动解析聊天消息。关闭后仅响应指令触发。 |
| `enable_emoji_reaction` | `true` | 是否开启表情回应。开启后插件处理磁链时会贴表情。 |
| `output_as_link` | `false` | 截图是否以直链形式发送。开启后不再下载发送图片。 |
| `max_screenshot_count`| `3` | 预览截图的最大数量 (0-5)。 |
| `cover_mosaic_level` | `0.3` | 预览图模糊程度 (0.0-1.0)。 |
| `max_magnet_count` | `1` | 单次消息最多解析的磁链数量。设置 >1 时结果将合并展示。 |

---

## 📝 更新日志

### **v1.2**

- 新增 表情回应
- 优化 指令功能

### **v1.1**

- 新增 `/磁链` 指令
- 新增 预览图模糊
- 新增 支持解析多条磁链
- 新增 群组白名单 等配置项

### **v1.0**

- 实现磁力链接基础正则提取与 API 解析功能
- 支持两种模式预览截图

---

## ⚠️ 注意事项

- 本插件使用 `https://whatslink.info` 接口，请确保 Bot 运行环境能够正常访问该地址。

---

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_magnet_preview/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>

