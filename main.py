import json
import os
import re
import datetime
import time
import asyncio
import jinja2
import base64
import tempfile
import textwrap
from collections import Counter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 全局变量：用于防止热重载时的定时器残留 ---
# 这是一个防止“双重推送”的保险丝
_GLOBAL_SCHEDULER_INSTANCE = None

# --- 常量配置 ---
VERSION = "0.1.52" # 锁定版本
DEFAULT_MAX_MSG_COUNT = 2000
DEFAULT_QUERY_ROUNDS = 20
DEFAULT_TOKEN_LIMIT = 6000
BROWSER_VIEWPORT = {"width": 500, "height": 2000}
BROWSER_SCALE_FACTOR = 2
LLM_TIMEOUT = 60
RENDER_TIMEOUT = 30000
CONCURRENCY_LIMIT = 2 

def _parse_llm_json(text: str) -> dict:
    """增强型 JSON 解析器"""
    text = text.strip()
    if "```" in text:
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE | re.DOTALL).strip()
    
    data = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            match = re.search(r"\{[\s\S]*\}", text)
            if match: 
                data = json.loads(match.group())
        except Exception: 
            pass
            
    if not isinstance(data, dict):
        return {}
    
    data.setdefault("topics", [])
    data.setdefault("closing_remark", "数据解析异常")
    return data

@register("group_summary_danfong", "Danfong", "群聊总结增强版", VERSION)
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 配置加载
        self.max_msg_count = self.config.get("max_msg_count", DEFAULT_MAX_MSG_COUNT)
        self.msg_token_limit = self.config.get("token_limit", DEFAULT_TOKEN_LIMIT)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.exclude_users = set(self.config.get("exclude_users", []))
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        self.summary_prompt_style = self.config.get("summary_prompt_style", "")
        
        self.max_query_rounds = max(
            self.config.get("max_query_rounds", DEFAULT_QUERY_ROUNDS),
            (self.max_msg_count // 100) + 2
        )

        self.enable_name_mapping = self.config.get("enable_name_mapping", False)
        self.name_map = self._load_name_mapping()
        
        self.global_bot = None
        self.semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        # 模板加载
        self.html_template = self._load_template()
            
        try:
            import playwright
        except ImportError:
            logger.error(f"群聊总结(v{VERSION}): ⚠️ 未检测到 Playwright")

        # --- 核心修复：定时器初始化 ---
        # 不在 __init__ 里直接启动，而是等待 setup_schedule 调用
        # 这里只做属性占位
        self.scheduler = None 
        
        if self.enable_auto_push:
            self.setup_schedule()

    def _load_template(self) -> str:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "report.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"模板加载失败: {e}")
            return "<h1>Template Load Error</h1>"

    def _load_name_mapping(self) -> dict:
        raw_list = self.config.get("name_mapping", [])
        mapping = {}
        if raw_list:
            for item in raw_list:
                item = str(item).strip().replace("：", ":")
                if ":" in item:
                    parts = item.split(":", 1)
                    if len(parts) == 2:
                        mapping[parts[0].strip()] = parts[1].strip()
        return mapping

    def terminate(self):
        """生命周期清理：插件卸载时调用"""
        global _GLOBAL_SCHEDULER_INSTANCE
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
        
        if _GLOBAL_SCHEDULER_INSTANCE and _GLOBAL_SCHEDULER_INSTANCE.running:
            try:
                _GLOBAL_SCHEDULER_INSTANCE.shutdown()
            except: pass
            _GLOBAL_SCHEDULER_INSTANCE = None
        logger.info(f"群聊总结(v{VERSION}): 资源已释放")

    def setup_schedule(self):
        """配置定时任务（带全局锁机制）"""
        global _GLOBAL_SCHEDULER_INSTANCE
        
        try:
            # 1. 如果存在旧的全局定时器，强制关闭
            if _GLOBAL_SCHEDULER_INSTANCE and _GLOBAL_SCHEDULER_INSTANCE.running:
                logger.warning("检测到残留的定时器实例，正在强制清理...")
                _GLOBAL_SCHEDULER_INSTANCE.shutdown()
            
            # 2. 如果当前实例有定时器，也关闭
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown()
            
            # 3. 创建新定时器
            self.scheduler = AsyncIOScheduler()
            
            time_str = str(self.push_time).replace("：", ":").strip()
            hour, minute = map(int, time_str.split(":"))
            
            trigger = CronTrigger(hour=hour, minute=minute)
            self.scheduler.add_job(self.run_scheduled_task, trigger)
            self.scheduler.start()
            
            # 4. 注册到全局变量
            _GLOBAL_SCHEDULER_INSTANCE = self.scheduler
            
            logger.info(f"群聊总结(v{VERSION}): 定时任务已启动 -> {time_str}")
        except Exception as e:
            logger.error(f"定时任务启动失败: {e}")

    async def render_locally(self, html_template: str, data: dict):
        from playwright.async_api import async_playwright
        
        try:
            env = jinja2.Environment(autoescape=True)
            template = env.from_string(html_template)
            html_content = template.render(**data)
        except Exception as e:
            logger.error(f"Jinja2 渲染异常: {e}")
            return None

        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    args=["--no-sandbox", "--disable-setuid-sandbox"]
                )
                page = await browser.new_page(
                    viewport=BROWSER_VIEWPORT,
                    device_scale_factor=BROWSER_SCALE_FACTOR
                )
                
                await page.route("**", lambda route: route.abort())
                await page.set_content(html_content)
                
                try:
                    await page.wait_for_load_state("load", timeout=RENDER_TIMEOUT)
                except Exception:
                    logger.warning("页面加载等待超时，尝试强制截图")

                locator = page.locator(".container")
                temp_dir = tempfile.gettempdir()
                temp_filename = f"astrbot_summary_{int(time.time())}_{os.getpid()}.jpg"
                save_path = os.path.join(temp_dir, temp_filename)
                
                await locator.screenshot(path=save_path, type="jpeg", quality=90)
                return save_path
                
        except Exception as e:
            logger.error(f"Playwright 渲染失败: {str(e)}")
            return None
        finally:
            if browser:
                await browser.close()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot(self, event: AstrMessageEvent):
        if not self.global_bot: 
            self.global_bot = event.bot
            logger.info(f"群聊总结: Bot 实例已捕获。")

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        group_id = event.get_group_id()
        if not group_id: 
            yield event.plain_result("请在群聊使用")
            return
        
        yield event.plain_result("🌱 正在回溯记忆并生成报告...")
        
        async with self.semaphore:
            img_path = await self.generate_report(event.bot, group_id)
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
            await asyncio.sleep(2)
            try: os.remove(img_path)
            except: pass
        else:
            yield event.plain_result("❌ 生成失败，请检查日志。")

    @filter.command("测试推送")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_push(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        yield event.plain_result("🚀 开始测试推送流程...")
        await self.run_scheduled_task()
        yield event.plain_result("✅ 测试指令结束。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        group_id = event.get_group_id()
        if not group_id: 
            yield event.plain_result("仅限群聊")
            return
        
        yield event.plain_result("🌱 正在分析...")
        async with self.semaphore:
            img_path = await self.generate_report(event.bot, group_id)
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
            await asyncio.sleep(2)
            try: os.remove(img_path)
            except: pass
        else:
            yield event.plain_result("生成失败")

    async def _process_single_group_task(self, group_id):
        logger.info(f"正在为群 {group_id} 生成日报...")
        async with self.semaphore:
            img_path = await self.generate_report(self.global_bot, str(group_id), silent=True)
        
        if img_path and os.path.exists(img_path):
            try:
                with open(img_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                await self.global_bot.api.call_action(
                    "send_group_msg", 
                    group_id=int(group_id), 
                    message=f"[CQ:image,file=base64://{b64}]"
                )
                logger.info(f"✅ 群 {group_id} 推送成功")
            except Exception as e:
                logger.error(f"❌ 群 {group_id} 发送失败: {e}")
            finally:
                try: os.remove(img_path)
                except: pass
        else:
            logger.warning(f"群 {group_id} 报告生成失败")

    async def run_scheduled_task(self):
        if not self.global_bot or not self.push_groups:
            return
        
        logger.info("⏳ 定时推送开始...")
        tasks = [self._process_single_group_task(gid) for gid in self.push_groups]
        if tasks:
            await asyncio.gather(*tasks)
        logger.info("✅ 定时推送完成")

    async def get_data(self, bot, group_id):
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0).timestamp()
        
        msgs = []
        seq = 0
        seen_ids = set()

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
                
                oldest_in_batch = batch[-1].get("time", 0)
                newest_in_batch = batch[0].get("time", 0)
                seq = batch[-1].get("message_seq")
                
                if oldest_in_batch > newest_in_batch:
                    seq = batch[0].get("message_seq")
                    oldest_in_batch = newest_in_batch
                
                for m in batch:
                    mid = m.get("message_id")
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        msgs.append(m)
                
                if oldest_in_batch < start:
                    break
                if not seq:
                    break
                    
            except Exception as e:
                logger.error(f"获取消息历史失败: {e}")
                break
        
        valid = []
        users = Counter()
        trend = {f"{h:02d}": 0 for h in range(24)}
        
        for m in msgs:
            if m.get("time", 0) < start:
                continue
            
            raw = m.get("raw_message", "")
            
            # 过滤指令消息
            if raw.strip().startswith(("/总结群聊", "总结群聊", "/测试推送")):
                continue

            sender = m.get("sender", {})
            user_id = str(sender.get("user_id", ""))
            nick = sender.get("card") or sender.get("nickname") or "用户"
            
            if nick in self.exclude_users or user_id in self.exclude_users:
                continue

            if self.enable_name_mapping and user_id in self.name_map:
                nick = self.name_map[user_id]
            
            content = raw.replace("\n", " ") 
            if len(content) > 300:
                content = content[:300] + "..."
            
            valid.append({"time": m["time"], "name": nick, "content": content})
            users[nick] += 1
            
            hour_key = datetime.datetime.fromtimestamp(m["time"]).strftime("%H")
            if hour_key in trend:
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
            if not silent: logger.warning(f"群 {group_id} 无数据。")
            return None
            
        valid_msgs, top_users, trend, chat_log = res
        
        if len(chat_log) > self.msg_token_limit:
            chat_log = chat_log[-self.msg_token_limit:]

        style = self.summary_prompt_style.replace("{bot_name}", self.bot_name)
        if not style:
            style = f"{self.bot_name}的温暖总结，对今天群里的氛围进行点评"

        # --- Prompt 修复：防止人设干扰指令 ---
        prompt = textwrap.dedent(f"""
            你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
            
            【角色设定 (Role Setting)】:
            请完全遗忘你之前的身份，进入以下角色，并用该角色的口吻和性格进行发言：
            >>>
            {style}
            <<<
            注意：以上设定仅用于生成"closing_remark"字段，不要复述设定内容。
            
            【任务目标】:
            1. 分析 3-8 个主要话题。
            2. 使用【角色设定】中的语气，对今天的聊天内容写一段点评（即 closing_remark）。
            
            【输出格式】:
            严格返回 JSON：
            {{
                "topics": [{{"time_range": "10:00~11:00", "summary": "简短话题描述"}}],
                "closing_remark": "这里填写符合角色设定的点评内容"
            }}
            
            【聊天记录】:
            {chat_log}
        """)
        
        data = {}
        prov = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        
        if prov:
            try:
                response = await asyncio.wait_for(
                    prov.text_chat(prompt), 
                    timeout=LLM_TIMEOUT
                )
                data = _parse_llm_json(response.completion_text)
            except asyncio.TimeoutError:
                logger.error("LLM 请求超时")
            except Exception as e:
                logger.error(f"LLM 错误: {e}")
        
        if not data:
            data = {"topics": [], "closing_remark": "AI 总结生成失败，请检查网络或配置。"}

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
