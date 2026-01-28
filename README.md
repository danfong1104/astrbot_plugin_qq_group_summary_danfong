

<div align="center">

# AstrBot Plugin: Group Summary (Danfong Enhanced Edition)

![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-purple?style=flat-square)
![Version](https://img.shields.io/badge/Version-0.1.52-blue?style=flat-square)
![Render](https://img.shields.io/badge/Render-Playwright-orange?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.9+-green?style=flat-square)

**一个更懂你的 QQ 群聊总结插件（本地高清渲染版）**
<br>
**定时自动推送** | **昵称映射** | **黑名单过滤** | **自定义人设** | **现代化 UI**

</div>

> [!IMPORTANT]
> 本插件是基于 [zhoufan47/astrbot_plugin_qq_group_summary](https://github.com/zhoufan47/astrbot_plugin_qq_group_summary) 的增强修改版。
> 彻底重构了渲染逻辑，解决了 Docker 环境下无法发送图片、远程接口报错等问题。

## ✨ 核心特性 (v0.1.52)

完美适配astrbot最新的4.12.4版本。相比原版，本插件经过深度重构，新增了以下核心能力：

1.  **🎨 本地高清渲染 (Local Render)**：
    * 抛弃不稳定的远程绘图接口，采用 **Playwright** 本地浏览器引擎。
    * 支持复杂的 CSS 毛玻璃、渐变特效，生成 **2倍超清** 长图。
    * **Docker 完美适配**：内置 Base64 图片传输协议，彻底解决 Docker/NAS 环境下图片发不出去的问题。
2.  **🎭 昵称映射 (Name Mapping)**：
    * 支持将 QQ 号强制映射为指定名称（如 `123456:张三`）。
    * 非常适合熟人小群或需要保护隐私的场景。
    * **智能容错**：自动识别中文/英文冒号，配置不再报错。
3.  **⏰ 每日定时推送 (Auto Push)**：
    * Bot 会在每天指定时间（如 23:00）自动向指定群发送日报。
    * 内置时区检测与调试日志，防止因服务器时区问题导致不推送。
4.  **🔧 调试与稳定性**：
    * 新增 `/测试推送` 指令，随时验证配置是否正确。
    * 自动裁剪空白区域，图片不再有大片留白。
5.  **🎨 现代化 UI**：
    * 紫韵流光风格，高端的渐变背景与动态排行榜。

## 🖼️ 效果预览

![b81fef4179777138df943f66909d5cb8](https://github.com/user-attachments/assets/2da7b76b-6765-484b-87f2-6ba09152755a)


## 🛠️ 安装说明 (非常重要！)

由于使用了本地浏览器渲染，**首次安装需要补全环境依赖**，否则无法出图。

### 1. 安装插件
将插件文件夹放入 `AstrBot/data/plugins/` 目录，或通过 AstrBot 后台上传安装。

### 2. 补全环境 (Docker / NAS 用户必读)
请进入 AstrBot 容器的终端（Terminal），依次执行以下两条命令：

```bash
# 1. 安装 Python 依赖库
pip install jinja2 playwright

# 2. 安装浏览器内核及系统依赖 (这一步最关键，耗时较长请耐心等待)
playwright install chromium --with-deps

```

> **注意**：如果不执行第 2 步，插件运行会报错提示找不到浏览器。

## ⚙️ 配置说明

安装插件后，请在 AstrBot WebUI 的插件配置页进行设置：

| 配置项 | 说明 | 示例/默认值 |
| --- | --- | --- |
| `enable_auto_push` | **定时推送开关** | `True` |
| `push_time` | **推送时间** (自动兼容中英文冒号) | `23:00` |
| `push_groups` | **推送群组** (群号列表) | `[12345678]` |
| `enable_name_mapping` | **昵称映射开关** (v0.1.51新增) | `True` |
| `name_mapping` | **映射表** (QQ号:新名字) | `["1001:张三", "1002:李四"]` |
| `exclude_users` | **黑名单** (不统计的用户昵称) | `["BOT", "管理员"]` |
| `max_msg_count` | 每次分析的最大消息量 | `2000` |
| `summary_prompt_style` | **Bot 人设** (自定义总结语提示词) | `以“{bot_name}”的毒舌口吻点评...` |

## 💻 指令列表

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/总结群聊` | 管理员 | 立即生成当前群的今日总结日报。 |
| `/测试推送` | 管理员 | **(调试用)** 强制触发一次定时推送流程，用于测试配置和发送通道是否正常。 |

## 📝 常见问题 (FAQ)

**Q: 定时任务不触发？**
A: 请看后台日志。插件启动时会打印 `当前系统时间`。如果 Docker 时区是 UTC（比北京时间慢8小时），请在配置时间上减去8小时（如想23点推，填15:00），或者调整 Docker 容器的 `TZ` 环境变量为 `Asia/Shanghai`。

**Q: 图片发不出来 / 报错 400？**
A: 确保你已经执行了 `playwright install chromium --with-deps`。v0.1.52 版本已经移除了所有远程接口，完全依赖本地环境。

**Q: 中文冒号报错？**
A: v0.1.52 版本已加入自动容错，无论是昵称映射还是推送时间，输入中文冒号 `：` 均可自动识别。

---

## 🤝 贡献与致谢

* Original Repo: [zhoufan47/astrbot_plugin_qq_group_summary](https://github.com/zhoufan47/astrbot_plugin_qq_group_summary)
* Enhanced by: Danfong
* Powered by AstrBot & Google Gemini & Playwright

```

```
