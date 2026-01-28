import json
import os
import re
import datetime
import time
import traceback
import asyncio
import jinja2
import base64
import tempfile
from collections import Counter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 常量定义 ---
DEFAULT_MAX_MSG_COUNT = 2000
DEFAULT_QUERY_ROUNDS = 10
DEFAULT_TOKEN_LIMIT = 6000
BROWSER_VIEWPORT = {"width": 500, "height": 2000}
BROWSER_SCALE_FACTOR = 2
LLM_TIMEOUT = 60  # LLM 请求超时时间（秒）
RENDER_TIMEOUT = 30000  # 渲染超时时间（毫秒）

def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    # 清洗 Markdown 代码块
    if "```" in text:
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE | re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            # 尝试提取 JSON 对象
            match = re.search(r"\{[\s\S]*\}", text)
            if match: 
                return json.loads(match.group())
        except Exception: 
            pass
    return {}

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.52")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 基础配置
        self.max_msg_count = self.config.get("max_msg_count", DEFAULT_MAX_MSG_COUNT)
        self.max_query_rounds = self.config.get("max_query_rounds", DEFAULT_QUERY_ROUNDS)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.msg_token_limit = self.config.get("token_limit", DEFAULT_TOKEN_LIMIT)
        self.exclude_users = self.config.get("exclude_users", [])
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        self.summary_prompt_style = self.config.get("summary_prompt_style", "")
        
        # 名称映射配置
        self.enable_name_mapping = self.config.get("enable_name_mapping", False)
        self.name_map = self._load_name_mapping()
        
        self.global_bot = None

        # 模板加载
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "report.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                self.html_template = f.read()
        except Exception as e:
            logger.error(f"群聊总结(增强版): 模板文件加载失败: {e}")
            self.html_template = "<h1>Template Load Error</h1>"
            
        # 依赖检测
        try:
            import playwright
            logger.info("群聊总结(增强版): 依赖环境检测正常。")
        except ImportError:
            logger.error("群聊总结(增强版): ⚠️ 未检测到 Playwright，请确保已执行安装命令。")

        # 定时任务初始化
        self.scheduler = AsyncIOScheduler()
        if self.enable_auto_push:
            self.setup_schedule()

    def _load_name_mapping(self) -> dict:
        """加载并处理昵称映射配置"""
        raw_mapping_list = self.config.get("name_mapping", [])
        mapping = {}
        if raw_mapping_list:
            for item in raw_mapping_list:
                item = str(item).strip().replace("：", ":")
                if ":" in item:
                    parts = item.split(":", 1)
                    qq_id = parts[0].strip()
                    new_name = parts[1].strip()
                    if qq_id and new_name:
                        mapping[qq_id] = new_name
            logger.info(f"群聊总结(增强版): 已加载 {len(mapping)} 个昵称映射规则。")
        return mapping

    def terminate(self):
        """插件卸载/重载时的生命周期清理"""
        try:
            if self.scheduler.running:
                self.scheduler.shutdown()
                logger.info("群聊总结(增强版): 定时任务调度器已关闭。")
        except Exception as e:
            logger.error(f"群聊总结(增强版): 资源清理异常: {e}")

    def setup_schedule(self):
        try:
            if self.scheduler.running:
                self.scheduler.shutdown()
            
            # 重新初始化调度器
            self.scheduler = AsyncIOScheduler()
            
            # 时间解析与容错
            time_str = str(self.push_time).replace("：", ":").strip()
            try:
                hour, minute = map(int, time_str.split(":"))
            except ValueError:
                logger.error(f"群聊总结(增强版): 推送时间格式错误 [{self.push_time}]，请使用 HH:MM 格式。")
                return

            trigger = CronTrigger(hour=hour, minute=minute)
            self.scheduler.add_job(self.run_scheduled_task, trigger)
            self.scheduler.start()
            
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            logger.info(f"群聊总结(增强版): 定时任务已启动 -> {time_str} (系统时间: {now_str})")
            
        except Exception as e:
            logger.error(f"群聊总结: 定时任务启动失败 {e}")

    async def render_locally(self, html_template: str, data: dict):
        """本地渲染 HTML 转图片"""
        from playwright.async_api import async_playwright
        
        # 1. 安全渲染 HTML (启用 autoescape 防止注入)
        try:
            # 使用 Jinja2 Environment 显式启用自动转义
            env = jinja2.Environment(autoescape=True)
            template = env.from_string(html_template)
            html_content = template.render(**data)
        except Exception as e:
            logger.error(f"模板渲染失败: {e}")
            return None

        browser = None
        try:
            async with async_playwright() as p:
                # 启动浏览器
                browser = await p.chromium.launch(
                    args=["--no-sandbox", "--disable-setuid-sandbox"]
                )
                page = await browser.new_page(
                    viewport=BROWSER_VIEWPORT,
                    device_scale_factor=BROWSER_SCALE_FACTOR
                )
                
                # 安全优化：拦截外部请求，防止 SSRF 和隐私泄露，同时加速渲染
                # 注意：如果模板依赖外部 CDN (如 marked.js)，需放行或改为本地注入
                # 这里为了兼容性暂时允许请求，但建议用户尽量本地化资源
                # await page.route("**", lambda route: route.abort()) 

                await page.set_content(html_content)
                
                # 使用 load 或 domcontentloaded 替代 networkidle，避免长连接导致超时
                try:
                    await page.wait_for_load_state("load", timeout=RENDER_TIMEOUT)
                except Exception:
                    logger.warning("页面加载等待超时，尝试强制截图")

                # 获取内容容器进行截图
                locator = page.locator(".container")
                
                # 使用系统临时目录，避免权限问题
                temp_dir = tempfile.gettempdir()
                temp_filename = f"astrbot_summary_{int(time.time())}.jpg"
                save_path = os.path.join(temp_dir, temp_filename)
                
                await locator.screenshot(path=save_path, type="jpeg", quality=90)
                return save_path
                
        except Exception as e:
            logger.error(f"本地渲染流程异常: {traceback.format_exc()}")
            return None
        finally:
            # 确保浏览器关闭
            if browser:
                await browser.close()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot(self, event: AstrMessageEvent):
        if not self.global_bot: 
            self.global_bot = event.bot
            logger.info(f"群聊总结(增强版): 已捕获 Bot 实例。")

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent):
        if not self.global_bot:
            self.global_bot = event.bot
        group_id = event.get_group_id()
        
        if not group_id: 
            yield event.plain_result("请在群聊使用")
            return
        
        yield event.plain_result("🌱 正在连接神经云端，回溯今日记忆...")
        img_path = await self.generate_report(event.bot, group_id)
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
            # 延迟清理
            await asyncio.sleep(2)
            try: 
                os.remove(img_path)
            except Exception: 
                pass
        else:
            yield event.plain_result("❌ 生成失败，请检查后台日志。")

    @filter.command("测试推送")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_push(self, event: AstrMessageEvent):
        if not self.global_bot: 
            self.global_bot = event.bot
        yield event.plain_result("🚀 正在手动触发推送任务...")
        await self.run_scheduled_task()
        yield event.plain_result("✅ 推送任务执行完毕。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent):
        if not self.global_bot:
            self.global_bot = event.bot
        group_id = event.get_group_id()
        
        if not group_id: 
            yield event.plain_result("仅限群聊")
            return
        
        yield event.plain_result("🌱 正在分析...")
        img_path = await self.generate_report(event.bot, group_id)
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
            await asyncio.sleep(2)
            try:
                os.remove(img_path)
            except Exception:
                pass
        else:
            yield event.plain_result("生成失败")

    async def run_scheduled_task(self):
        if not self.global_bot or not self.push_groups:
            return
        logger.info("⏳ 定时器触发，开始推送...")
        
        for gid in self.push_groups:
            img_path = await self.generate_report(self.global_bot, str(gid), silent=True)
            if img_path and os.path.exists(img_path):
                try:
                    # 使用 Base64 发送以兼容 Docker/跨容器环境
                    with open(img_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    
                    await self.global_bot.api.call_action(
                        "send_group_msg", 
                        group_id=int(gid), 
                        message=f"[CQ:image,file=base64://{b64}]"
                    )
                    logger.info(f"✅ 群 {gid} 推送成功")
                except Exception as e:
                    logger.error(f"❌ 群 {gid} 发送失败: {e}")
                
                try: 
                    os.remove(img_path)
                except Exception: 
                    pass
            await asyncio.sleep(5)

    async def get_data(self, bot, group_id):
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0).timestamp()
        
        msgs = []
        seq = 0
        seen_ids = set() # 用于消息去重

        # 分页获取消息
        for _ in range(self.max_query_rounds):
            if len(msgs) >= self.max_msg_count:
                break
            try:
                ret = await bot.api.call_action(
                    "get_group_msg_history", 
                    group_id=group_id, 
                    count=100, 
                    message_seq=seq, 
                    reverseOrder=True
                )
                batch = ret.get("messages", [])
                if not batch:
                    break
                
                # 更新游标
                oldest_in_batch = batch[-1].get("time", 0)
                newest_in_batch = batch[0].get("time", 0)
                
                # 处理 OneBot 实现差异导致的 seq 逻辑
                seq = batch[-1].get("message_seq")
                if oldest_in_batch > newest_in_batch:
                    seq = batch[0].get("message_seq")
                    oldest_in_batch = newest_in_batch
                
                # 消息处理与去重
                for m in batch:
                    msg_id = m.get("message_id")
                    if msg_id and msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        msgs.append(m)
                
                if oldest_in_batch < start:
                    break
            except Exception as e:
                logger.error(f"获取群历史消息异常: {e}")
                break
        
        valid = []
        users = Counter()
        trend = Counter()
        
        for m in msgs:
            # 再次校验时间，确保准确
            if m.get("time", 0) < start:
                continue
            
            raw = m.get("raw_message", "")
            
            # --- 名称获取与映射 ---
            sender_info = m.get("sender", {})
            user_id = str(sender_info.get("user_id", ""))
            
            # 优先获取群名片，其次昵称
            nick = sender_info.get("card") or sender_info.get("nickname") or "用户"
            
            # 1. 黑名单过滤 (优先匹配原始昵称/QQ)
            if nick in self.exclude_users or user_id in self.exclude_users:
                continue

            # 2. 名称映射 (处理后如果还在黑名单，这属于配置逻辑问题，暂不二次过滤)
            if self.enable_name_mapping and user_id in self.name_map:
                nick = self.name_map[user_id]
            # ---------------------
            
            # 内容截断优化：简单截断，防止过长
            # 注意：若截断处正好在 CQ 码中间可能导致乱码，但为了 Token 限制需做取舍
            content = raw.replace("\n", " ") 
            if len(content) > 200:
                content = content[:200] + "..."
            
            valid.append({"time": m["time"], "name": nick, "content": content})
            users[nick] += 1
            
            # 修复 Trend Key 格式：统一为两位数小时字符串
            hour_key = datetime.datetime.fromtimestamp(m["time"]).strftime("%H")
            trend[hour_key] += 1
            
        valid.sort(key=lambda x: x["time"])
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(v['time']).strftime('%H:%M')}] {v['name']}: {v['content']}" 
            for v in valid
        ])
        
        return valid, [{"name": k, "count": v} for k,v in users.most_common(5)], trend, chat_log

    async def generate_report(self, bot, group_id, silent=False):
        try:
            info = await bot.api.call_action("get_group_info", group_id=group_id)
        except Exception:
            info = {"group_name": "群聊"}
        
        res = await self.get_data(bot, group_id)
        if not res or not res[0]:
            if not silent:
                logger.warning(f"群 {group_id} 无有效数据或获取失败")
            return None
            
        valid_msgs, top_users, trend, chat_log = res
        
        # Token/字符截断
        if len(chat_log) > self.msg_token_limit:
            chat_log = chat_log[-self.msg_token_limit:]

        style = self.summary_prompt_style.replace("{bot_name}", self.bot_name) or f"{self.bot_name}的温暖总结"
        
        # 安全提示：LLM Prompt 注入风险提示，建议用户在 style 中不要信任输入
        prompt = f"""
        你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
        
        【要求】：
        1. 分析 3-8 个主要话题，每个话题包含：时间段（如 10:00 ~ 11:00）和简短内容。
        2. {style}
        3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}
        
        【聊天记录】：
        {chat_log}
        """
        
        data = {}
        prov = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        
        if prov:
            try:
                # 增加超时控制
                response = await asyncio.wait_for(
                    prov.text_chat(prompt), 
                    timeout=LLM_TIMEOUT
                )
                data = _parse_llm_json(response.completion_text)
            except asyncio.TimeoutError:
                logger.error("LLM 请求超时")
            except Exception as e:
                logger.error(f"LLM 交互异常: {e}")
        
        if not data:
            data = {"topics": [], "closing_remark": "分析失败，可能是 LLM 响应超时或格式错误。"}

        render_data = {
            "date": datetime.datetime.now().strftime("%Y.%m.%d"),
            "top_users": top_users,
            "trend": trend,
            "topics": data.get("topics", []),
            "summary_text": data.get("closing_remark", ""),
            "group_name": info.get("group_name"),
            "bot_name": self.bot_name
        }
        
        return await self.render_locally(self.html_template, render_data)
