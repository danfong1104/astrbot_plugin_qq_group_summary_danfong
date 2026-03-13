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
import html
import urllib.parse
from collections import Counter
from typing import List, Dict, Tuple, Optional, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 全局变量 ---
_GLOBAL_SCHEDULER_INSTANCE = None

# --- 常量配置 (全面补齐) ---
VERSION = "0.1.59" 
DEFAULT_MAX_MSG_COUNT = 2000
DEFAULT_QUERY_ROUNDS = 20
DEFAULT_TOKEN_LIMIT = 6000
BROWSER_VIEWPORT = {"width": 500, "height": 2000}
BROWSER_SCALE_FACTOR = 1.5
RENDER_TIMEOUT = 30000
CONCURRENCY_LIMIT = 2 

# 核心调度常量
MAX_RETRY_ATTEMPTS = 3
LLM_TIMEOUT = 180
API_TIMEOUT = 30
RETRY_BASE_DELAY = 2.0
HISTORY_FETCH_BATCH_SIZE = 100
ESTIMATED_CHARS_PER_TOKEN = 2
OVERHEAD_CHARS_PER_MSG = 15
PUSH_DELAY_BETWEEN_GROUPS = 5.0

# API 名称常量
API_GET_GROUP_MSG_HISTORY = "get_group_msg_history"
API_GET_GROUP_INFO = "get_group_info"
API_SEND_GROUP_MSG = "send_group_msg"

PLATFORM_ONEBOT = ("qq", "onebot", "aiocqhttp", "napcat", "llonebot")
PLATFORM_UNSUPPORTED = ("telegram", "discord", "wechat")

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
            match = re.search(r"(\{[\s\S]*\})", text)
            if match: 
                data = json.loads(match.group())
        except Exception: 
            pass
            
    if not isinstance(data, dict):
        raise ValueError("解析出非字典结构")
    return data

@register("group_summary_danfong", "Danfong", "群聊总结增强版", VERSION)
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
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
        self._bot_lock = asyncio.Lock()
        self._group_locks: Dict[str, asyncio.Lock] = {}
        self.semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        self.html_template = self._load_template()
            
        try:
            import playwright
        except ImportError:
            logger.error(f"群聊总结(v{VERSION}): ⚠️ 未检测到 Playwright")

        self.scheduler = None 
        
        if self.enable_auto_push:
            self.setup_schedule()

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

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
        global _GLOBAL_SCHEDULER_INSTANCE
        try:
            if _GLOBAL_SCHEDULER_INSTANCE and _GLOBAL_SCHEDULER_INSTANCE.running:
                _GLOBAL_SCHEDULER_INSTANCE.shutdown()
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown()
            
            self.scheduler = AsyncIOScheduler()
            time_str = str(self.push_time).replace("：", ":").strip()
            hour, minute = map(int, time_str.split(":"))
            
            trigger = CronTrigger(hour=hour, minute=minute)
            self.scheduler.add_job(self.run_scheduled_task, trigger)
            self.scheduler.start()
            
            _GLOBAL_SCHEDULER_INSTANCE = self.scheduler
            logger.info(f"群聊总结(v{VERSION}): 定时任务已启动 -> {time_str}")
        except Exception as e:
            logger.error(f"定时任务启动失败: {e}")

    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        async with self._bot_lock:
            if event and event.bot:
                self.global_bot = event.bot
                return event.bot
            if self.global_bot:
                return self.global_bot
            try:
                if hasattr(self.context, "get_bots"):
                    bots = self.context.get_bots()
                    if bots:
                        for bot in bots.values():
                            p_name = getattr(bot, "platform_name", "").lower()
                            if any(k in p_name for k in PLATFORM_ONEBOT):
                                self.global_bot = bot
                                return bot
                        self.global_bot = next(iter(bots.values()))
                        return self.global_bot
            except Exception:
                pass
            return None

    # ================= 强力本地渲染 (使用 Playwright) =================
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
                browser = await p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
                page = await browser.new_page(viewport=BROWSER_VIEWPORT, device_scale_factor=BROWSER_SCALE_FACTOR)
                await page.route("**", lambda route: route.abort())
                await page.set_content(html_content)
                
                try:
                    await page.wait_for_load_state("load", timeout=RENDER_TIMEOUT)
                except Exception:
                    pass

                locator = page.locator(".container")
                temp_dir = tempfile.gettempdir()
                temp_filename = f"astrbot_summary_{int(time.time())}_{os.getpid()}.jpg"
                save_path = os.path.join(temp_dir, temp_filename)
                
                await locator.screenshot(path=save_path, type="jpeg", quality=80)
                return save_path
        except Exception as e:
            logger.error(f"Playwright 渲染失败: {str(e)}")
            return None
        finally:
            if browser:
                await browser.close()

    # ================= 数据获取与清洗 =================
    async def _fetch_messages(self, bot, group_id: str, start_ts: float) -> List[dict]:
        all_msgs = []
        msg_seq = 0
        last_ids = set()
        last_min_seq = None 

        for round_i in range(self.max_query_rounds):
            if len(all_msgs) >= self.max_msg_count: break
            try:
                resp = await asyncio.wait_for(
                    bot.api.call_action(API_GET_GROUP_MSG_HISTORY, 
                        group_id=group_id, count=HISTORY_FETCH_BATCH_SIZE, 
                        message_seq=msg_seq, reverseOrder=True),
                    timeout=API_TIMEOUT
                )
                if not resp or "messages" not in resp: break
                batch = sorted(resp["messages"], key=lambda x: x.get('time', 0), reverse=True)
                if not batch: break

                oldest = batch[-1]
                curr_seq = oldest.get('message_seq')
                curr_time = oldest.get('time', 0)
                
                if curr_seq is None or (last_min_seq is not None and curr_seq >= last_min_seq): break
                last_min_seq = curr_seq
                msg_seq = curr_seq

                for m in batch:
                    mid = m.get('message_id')
                    if not mid: continue
                    if mid not in last_ids:
                        all_msgs.append(m)
                        last_ids.add(mid)
                
                if curr_time <= start_ts: break
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        return all_msgs

    def _process_data(self, messages: List[dict], start_ts: float) -> Tuple[List[dict], List[dict], Dict[str, int], str]:
        valid_msgs = []
        user_stats = Counter() 
        trend_stats = {f"{h:02d}": 0 for h in range(24)}
        char_limit = self.msg_token_limit * ESTIMATED_CHARS_PER_TOKEN
        
        for m in messages:
            if m.get('time', 0) < start_ts: continue
            raw = m.get('raw_message', "")
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw).strip()
            if not content: continue
            if content.startswith(("/总结群聊", "总结群聊", "/测试推送")): continue
            
            sender = m.get('sender', {})
            nick = sender.get('card') or sender.get('nickname') or "未知用户"
            uid = str(sender.get('user_id', ''))
            
            if nick in self.exclude_users or uid in self.exclude_users: continue
            if self.enable_name_mapping and uid in self.name_map: nick = self.name_map[uid]

            content = content.replace("\n", " ") 
            if len(content) > 300: content = content[:300] + "..."
            
            user_stats[nick] += 1
            hour = datetime.datetime.fromtimestamp(m['time']).strftime("%H")
            if hour in trend_stats: trend_stats[hour] += 1
            valid_msgs.append({"time": m['time'], "name": nick, "content": content})

        top_users = [{"name": html.escape(k), "count": v} for k, v in user_stats.most_common(5)]
        valid_msgs.sort(key=lambda x: x['time'])
        
        chat_lines = [f"[{datetime.datetime.fromtimestamp(v['time']).strftime('%H:%M')}] {v['name']}: {v['content']}" for v in valid_msgs]
        
        total_len = sum(len(line) for line in chat_lines)
        if total_len > char_limit:
            meaningful_lines = [line for line in chat_lines if len(line.split(":", 1)[-1].strip()) > 2]
            total_len = sum(len(line) for line in meaningful_lines)
            
            if total_len > char_limit:
                ratio = char_limit / total_len
                keep_count = max(10, int(len(meaningful_lines) * ratio))
                step = len(meaningful_lines) / keep_count
                chat_lines = [meaningful_lines[int(i * step)] for i in range(keep_count)]
            else:
                chat_lines = meaningful_lines

        chat_log = "\n".join(chat_lines)
        return valid_msgs, top_users, trend_stats, chat_log

    # ================= LLM 分析 =================
    async def _run_llm_with_debug(self, chat_log: str) -> Tuple[Optional[dict], str]:
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider:
            return None, "❌ 致命错误: AstrBot 未配置任何可用的大模型 (Provider)。"

        style = self.summary_prompt_style.replace("{bot_name}", self.bot_name)
        if not style:
            style = f"{self.bot_name}的温暖总结，对今天群里的氛围进行点评"

        prompt = textwrap.dedent(f"""
            你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份精简的总结数据。
            
            【角色设定 (Role Setting)】:
            请完全遗忘你之前的身份，进入以下角色，并用该角色的口吻和性格进行发言：
            >>>
            {style}
            <<<
            
            【任务目标】:
            1. 分析提炼出 1-8 个核心话题。哪怕记录里只有 1 句话，也要为这 1 句话生成 1 个话题并按格式输出！
            2. 使用【角色设定】中的语气写一段点评（即 closing_remark）。
            3. **必须**在话题摘要中包含**参与讨论的主要群友昵称**。
            
            【输出格式】 (绝对不要输出 Markdown 代码块，必须直接返回 JSON):
            {{
                "topics": [{{"time_range": "起始时间~结束时间", "summary": "【参与者人名】+ 事件摘要"}}],
                "closing_remark": "这里填写符合角色设定的点评内容"
            }}
            
            【聊天记录】:
            {chat_log}
        """)

        last_error = "未知错误"
        for i in range(MAX_RETRY_ATTEMPTS):
            try:
                if i > 0: await asyncio.sleep(RETRY_BASE_DELAY)
                response = await asyncio.wait_for(provider.text_chat(prompt), timeout=LLM_TIMEOUT)
                
                if not response or not response.completion_text:
                    last_error = f"第 {i+1} 次请求: 大模型返回了空白内容！"
                    continue
                
                try:
                    data = _parse_llm_json(response.completion_text)
                    if isinstance(data, dict) and "topics" in data:
                        return data, ""
                except Exception as parse_e:
                    bad_text = response.completion_text.replace('\n', ' ')[:50]
                    last_error = f"第 {i+1} 次解析失败: 模型未返回标准 JSON。模型实际回复了: '{bad_text}...' (异常: {parse_e})"
                    continue

            except asyncio.TimeoutError:
                last_error = f"第 {i+1} 次请求: 大模型在 {LLM_TIMEOUT} 秒内未响应 (Timeout)。"
            except Exception as e:
                last_error = f"第 {i+1} 次请求: 接口报错 -> {str(e)}"
                
        return None, last_error

    # ================= 主流程 =================
    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        try:
            today_ts = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            try:
                g_info = await asyncio.wait_for(bot.api.call_action(API_GET_GROUP_INFO, group_id=str(group_id)), timeout=5)
            except:
                g_info = {"group_name": "群聊"}

            raw_msgs = await self._fetch_messages(bot, str(group_id), today_ts)
            if not raw_msgs:
                if not silent: raise RuntimeError("群内暂无有效记录。")
                return None

            _, top_users, trend, chat_log = self._process_data(raw_msgs, today_ts)
            if not chat_log:
                if not silent: raise RuntimeError("过滤后无有效文本消息。")
                return None

            data, err_reason = await self._run_llm_with_debug(chat_log)
            
            if not data:
                data = {
                    "topics": [{"time_range": "错误诊断", "summary": "开发者请看下方的调试信息 👇"}], 
                    "closing_remark": f"🚨 **大模型调用失败！**\n\n**详细诊断：**\n{err_reason}\n\n*请排查 AstrBot 设置。*"
                }

            render_data = {
                "date": datetime.datetime.now().strftime("%Y.%m.%d"),
                "top_users": top_users,
                "trend": trend,
                "topics": data.get("topics", []),
                "summary_text": data.get("closing_remark", ""),
                "group_name": html.escape(g_info.get("group_name", "群聊")),
                "bot_name": self.bot_name
            }
            
            return await self.render_locally(self.html_template, render_data)
        except Exception as e:
            if not silent: raise e
            return None

    # ================= 交互入口 =================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot(self, event: AstrMessageEvent):
        if not self.global_bot: 
            self.global_bot = event.bot

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, payload=None, **kwargs):
        try:
            bot = await self._get_bot(event)
            gid = event.get_group_id()
            if not gid:
                yield event.plain_result("⚠️ 请在群内使用")
                return

            yield event.plain_result(f"🌱 正在分析今日群聊记录...")
            
            lock = self._get_group_lock(str(gid))
            if lock.locked():
                yield event.plain_result("⚠️ 任务进行中，请勿重复触发...")
                return

            async with lock:
                img_path = await self.generate_report(bot, gid, silent=False)
            
            if img_path and os.path.exists(img_path):
                yield event.image_result(img_path)
                await asyncio.sleep(2)
                try: os.remove(img_path)
                except: pass
            else:
                yield event.plain_result(f"❌ 结果为空，可能是图片渲染失败。")

        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"[{VERSION}] 错误:\n{err}")
            yield event.plain_result(f"❌ 运行报错: {e}")

    @filter.command("测试推送")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_push(self, event: AstrMessageEvent, payload=None, **kwargs):
        if not self.global_bot: self.global_bot = event.bot
        yield event.plain_result("🚀 开始测试推送流程...")
        await self.run_scheduled_task()
        yield event.plain_result("✅ 测试指令结束。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, payload=None, **kwargs):
        try:
            bot = await self._get_bot(event)
            gid = event.get_group_id()
            if not gid: return

            yield event.plain_result(f"🌱 正在分析...")
            async with self._get_group_lock(str(gid)):
                img_path = await self.generate_report(bot, gid, silent=False)
                
            if img_path and os.path.exists(img_path):
                yield event.image_result(img_path)
                await asyncio.sleep(2)
                try: os.remove(img_path)
                except: pass
        except Exception as e:
            logger.error(f"Tool Error: {e}")

    # ================= 定时任务 =================
    async def run_scheduled_task(self):
        if not self.global_bot or not self.push_groups: return
        logger.info("⏳ 定时推送开始...")
        tasks = [self._process_single_group_task(gid) for gid in self.push_groups]
        if tasks:
            await asyncio.gather(*tasks)
        logger.info("✅ 定时推送完成")

    async def _process_single_group_task(self, group_id):
        logger.info(f"正在为群 {group_id} 生成日报...")
        async with self.semaphore:
            img_path = await self.generate_report(self.global_bot, str(group_id), silent=True)
        
        if img_path and os.path.exists(img_path):
            try:
                with open(img_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                await self.global_bot.api.call_action(
                    API_SEND_GROUP_MSG, 
                    group_id=int(group_id), 
                    message=f"[CQ:image,file=base64://{b64}]"
                )
                logger.info(f"✅ 群 {group_id} 推送成功")
            except Exception as e:
                logger.error(f"❌ 群 {group_id} 发送失败: {e}")
            finally:
                try: os.remove(img_path)
                except: pass
