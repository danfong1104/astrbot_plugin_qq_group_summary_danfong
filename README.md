<div align="center">

# AstrBot Plugin: Group Summary (Danfong Enhanced Edition)

![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-purple)
![Version](https://img.shields.io/badge/Version-0.1.37-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)

**一个更懂你的 QQ 群聊总结插件**
<br>
支持 **定时自动推送** | **黑名单过滤** | **自定义总结风格** | **现代化 UI**

</div>

> [!NOTE]
> 本插件是基于 [zhoufan47/astrbot_plugin_qq_group_summary](https://github.com/zhoufan47/astrbot_plugin_qq_group_summary) 的增强修改版。
> 感谢原作者 **zhoufan47** 和 **Loping151** 的灵感与开源贡献。

## ✨ 特色功能 (增强版独占)

相比原版，本插件新增了以下核心能力：

1.  **⏰ 每日定时推送**：无需手动指令，Bot 会在每天指定时间（如 23:00）自动向指定群发送今日日报。
2.  **🚫 用户黑名单**：支持在配置中屏蔽特定用户（如 Bot 自己或其他刷屏用户），让统计数据更纯净。
3.  **🎭 自定义 Bot 人设**：想让 Bot 变成“毒舌评论员”还是“温柔知心姐姐”？在配置里写段 Prompt 就能实现！
4.  **🎨 现代化 UI 设计**：
    * **紫韵流光风格**：高端的渐变背景与毛玻璃卡片效果。
    * **荣耀排行榜**：前三名拥有专属奖牌图标和高光特效。
    * **时间轴布局**：话题概览采用直观的时间轴设计。
    * **手机端适配**：专为手机 QQ 优化的排版和字号。
5.  **🔧 稳定性增强**：
    * 自动处理 Docker 环境下的图片发送问题（Base64 转码）。
    * 自动捕获 Bot 实例，确保定时任务稳定运行。
    * 支持解析网络图片 URL。

## 🖼️ 效果预览

![Uploading 8f5964c42a65a59b3cd1144ae0f3a1a8.jpeg…]()


## ⚙️ 配置说明

安装插件后，请在 AstrBot WebUI 的插件配置页进行设置：

| 配置项 | 说明 | 示例/默认值 |
| :--- | :--- | :--- |
| `max_msg_count` | 每次分析的最大消息数量 | `2000` |
| `provider_id` | 指定使用的 LLM 模型 | (留空则使用全局默认) |
| `exclude_users` | **黑名单**：不参与统计的用户昵称列表 | `["BOT", "管理员"]` |
| `enable_auto_push` | **定时推送开关**：是否开启每日自动发送 | `True` |
| `push_time` | **推送时间**：24小时制 HH:MM | `23:00` |
| `push_groups` | **推送群组**：需要自动发送的群号列表 | `[12345678, 87654321]` |
| `summary_prompt_style` | **Bot 人设风格**：自定义总结语的提示词 | `以“{bot_name}”的毒舌口吻，嘲讽一下大家今天的摸鱼行为。` |

## 🚀 快速开始

1.  将本插件文件夹放入 `AstrBot/data/plugins/` 目录。
2.  重启 AstrBot。
3.  在 WebUI 中启用插件并完成配置。
4.  **重要**：Bot 启动后，需要至少在一个群里收到过一条消息（任意消息），以激活后台推送服务。

## 📝 贡献与致谢

* Original Repo: [zhoufan47/astrbot_plugin_qq_group_summary](https://github.com/zhoufan47/astrbot_plugin_qq_group_summary)
* Enhanced by: Danfong
* Powered by AstrBot & Google Gemini

---
