import json
import os
import re
import datetime
import traceback
import asyncio
import base64
import html
import urllib.parse
import textwrap
from pathlib import Path
from collections import Counter
from typing import List, Dict, Tuple, Optional, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 版本强制标识 ---
VERSION = "0.1.37-FixParam"

# 常量
API_GET_GROUP_MSG_HISTORY = "get_group_msg_history"
API_GET_GROUP_INFO = "get_group_info"
API_SEND_GROUP_MSG = "send_group_msg"
MAX_RETRY_ATTEMPTS = 3
LLM_TIMEOUT = 60
API_TIMEOUT = 30
RETRY_BASE_DELAY = 2.0
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
ESTIMATED_CHARS_PER_TOKEN = 2
HISTORY_FETCH_BATCH_SIZE = 200
OVERHEAD_CHARS_PER_MSG = 15
PLATFORM_ONEBOT = ("qq", "onebot", "aiocqhttp", "napcat", "llonebot")
PLATFORM_UNSUPPORTED = ("telegram", "discord", "wechat")

def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        stack = 0; start = -1
        for i, char in enumerate(text):
            if char == '{':
                if stack == 0: start = i
                stack += 1
            elif char == '}':
                stack -= 1
                if stack == 0: return json.loads(text[start:i+1])
    except Exception:
        pass
    raise ValueError(f"JSON解析失败: {text[:50]}...")

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.37")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        self.max_msg_count = self.config.get("max_msg_count", 2000)
        self.max_query_rounds = self.config.get("max_query_rounds", 10)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.msg_token_limit = self.config.get("token_limit", 6000)
        self.exclude_users = set(self.config.get("exclude_users", []))
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        
        raw_style = self.config.get("summary_prompt_style", "")
        self.prompt_style = raw_style.replace("{bot_name}", self.bot_name) if raw_style else \
                            f"写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。"
        
        self._global_bot = None
        self._bot_lock = asyncio.Lock()
        self._group_locks: Dict[str, asyncio.Lock] = {}
        self.scheduler = None 
        self._push_semaphore = asyncio.Semaphore(3)

        self.template_path = Path(__file__).parent / "templates" / "report.html"
        self.html_template = self._load_template()

        # 打印强制日志证明代码已更新
        logger.warning(f"==========================================")
        logger.warning(f"群聊总结插件已加载 - 版本: {VERSION}")
        logger.warning(f"==========================================")

        if self.enable_auto_push:
            self.setup_schedule()

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    def _load_template(self) -> str:
        try:
            if not self.template_path.exists():
                return "<h1>Template Missing</h1>"
            return self.template_path.read_text(encoding="utf-8")
        except Exception:
            return "<h1>Template Load Error</h1>"

    def setup_schedule(self):
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=True)
            self.scheduler = AsyncIOScheduler()
            try:
                hour, minute = self.push_time.split(":")
                trigger = CronTrigger(hour=int(hour), minute=int(minute))
                self.scheduler.add_job(self.run_scheduled_task, trigger)
                self.scheduler.start()
                logger.info(f"[{VERSION}] 定时任务启动 -> {self.push_time}")
            except ValueError:
                logger.error(f"[{VERSION}] 时间格式错误")
        except Exception as e:
            logger.error(f"[{VERSION}] 调度器失败: {e}")

    def terminate(self):
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception:
            pass

    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        async with self._bot_lock:
            if event and event.bot:
                self._global_bot = event.bot
                return event.bot
            if self._global_bot:
                return self._global_bot
            try:
                if hasattr(self.context, "get_bots"):
                    bots = self.context.get_bots()
                    if bots:
                        for bot in bots.values():
                            p_name = getattr(bot, "platform_name", "").lower()
                            if any(k in p_name for k in PLATFORM_ONEBOT):
                                self._global_bot = bot
                                return bot
                        self._global_bot = next(iter(bots.values()))
                        return self._global_bot
            except Exception:
                pass
            return None

    async def html_render(self, template: str, data: dict, options: dict = None) -> Optional[str]:
        try:
            if hasattr(self.context, "image_renderer"):
                return await self.context.image_renderer.render(template, data, **(options or {}))
            return None
        except Exception as e:
            logger.error(f"Render Error: {e}")
            return None

    # ================= 消息处理 =================
    async def _fetch_messages(self, bot, group_id: str, start_ts: float) -> List[dict]:
        all_msgs = []
        msg_seq = 0
        last_ids = set()
        last_min_seq = None 

        for _ in range(self.max_query_rounds):
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
        trend_stats = Counter()
        char_limit = self.msg_token_limit * ESTIMATED_CHARS_PER_TOKEN
        
        for m in messages:
            if m.get('time', 0) < start_ts: continue
            raw = m.get('raw_message', "")
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw).strip()
            if not content: continue
            
            sender = m.get('sender', {})
            nick = sender.get('card') or sender.get('nickname') or "未知用户"
            uid = str(sender.get('user_id', ''))
            
            if nick in self.exclude_users or uid in self.exclude_users: continue

            user_stats[nick] += 1
            hour = datetime.datetime.fromtimestamp(m['time']).strftime("%H")
            trend_stats[hour] += 1
            valid_msgs.append({"time": m['time'], "name": nick, "content": content})

        top_users = [{"name": html.escape(k), "count": v} for k, v in user_stats.most_common(5)]
        valid_msgs.sort(key=lambda x: x['time'])
        
        acc_chars = 0
        final_msgs = []
        for msg in reversed(valid_msgs):
            cost = len(msg['content']) + len(msg['name']) + OVERHEAD_CHARS_PER_MSG
            if acc_chars + cost > char_limit: break
            final_msgs.insert(0, msg) 
            acc_chars += cost
        
        chat_log = "\n".join([f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}" for m in final_msgs])
        return valid_msgs, top_users, dict(trend_stats), chat_log

    async def _run_llm(self, chat_log: str) -> Optional[dict]:
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider: return None

        prompt = textwrap.dedent(f"""
        角色：{self.bot_name}。任务：群聊总结。
        要求：
        1. 提取3-8个话题(时间段+摘要)。
        2. {self.prompt_style}
        3. 严禁包含Markdown代码块标记，直接返回JSON对象。
        格式：{{"topics": [{{"time_range":"", "summary":""}}], "closing_remark":""}}
        记录：
        <chat_logs>
        {chat_log}
        </chat_logs>
        """).strip()

        for i in range(MAX_RETRY_ATTEMPTS):
            try:
                if i > 0: await asyncio.sleep(RETRY_BASE_DELAY * (2 ** i))
                resp = await asyncio.wait_for(provider.text_chat(prompt, session_id=None), timeout=LLM_TIMEOUT)
                if resp and resp.completion_text:
                    data = _parse_llm_json(resp.completion_text)
                    if isinstance(data, dict): return data
            except Exception as e:
                logger.error(f"[{VERSION}] LLM Attempt {i+1} Failed: {e}")
        return None

    # ================= 流程总控 =================
    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        try:
            today_ts = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            try:
                g_info = await asyncio.wait_for(bot.api.call_action(API_GET_GROUP_INFO, group_id=str(group_id)), timeout=5)
            except:
                g_info = {"group_name": "群聊"}

            raw_msgs = await self._fetch_messages(bot, str(group_id), today_ts)
            if not raw_msgs:
                if not silent: logger.warning(f"[{VERSION}] No messages")
                return None

            _, top_users, trend, chat_log = self._process_data(raw_msgs, today_ts)
            if not chat_log:
                if not silent: logger.warning(f"[{VERSION}] No valid text")
                return None

            analysis = await self._run_llm(chat_log)
            if not analysis:
                analysis = {"topics": [], "closing_remark": "分析超时。"}

            render_data = {
                "date": datetime.datetime.now().strftime("%Y.%m.%d"),
                "top_users": top_users,
                "trend": trend,
                "topics": analysis.get("topics", []),
                "summary_text": analysis.get("closing_remark", ""),
                "group_name": html.escape(g_info.get("group_name", "群聊")),
                "bot_name": self.bot_name
            }
            return await self.html_render(self.html_template, render_data, options={"quality": 95, "viewport_width": 500})
        except Exception as e:
            # 捕获异常并向上抛出，以便指令入口生成图片
            raise e

    # ================= 修复指令入口 =================
    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, message: str = ""):
        """
        手动指令：/总结群聊
        FIX: 使用 message: str = "" 是 AstrBot 最标准的参数写法。
        解析器看到有默认值，就不会报错参数缺失。
        """
        logger.info(f"[{VERSION}] 手动触发收到请求")
        
        try:
            bot = await self._get_bot(event)
            gid = event.get_group_id()
            if not gid:
                yield event.plain_result("⚠️ 请在群内使用")
                return

            yield event.plain_result(f"🌱 正在分析今日 {gid} 的聊天记录...")
            
            lock = self._get_group_lock(str(gid))
            if lock.locked():
                yield event.plain_result("⚠️ 任务进行中...")
                return

            async with lock:
                img = await self.generate_report(bot, gid, silent=False)
            
            if img:
                yield event.image_result(img)
            else:
                yield event.plain_result(f"❌ 生成失败，请检查日志 [{VERSION}]")

        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"[{VERSION}] 手动触发崩溃:\n{err}")
            # 发送错误详情到群里，方便调试
            yield event.plain_result(f"❌ 插件内部错误 (v{VERSION}):\n{e}")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, message: str = ""):
        try:
            bot = await self._get_bot(event)
            gid = event.get_group_id()
            if not gid: return

            yield event.plain_result(f"🌱 正在分析...")
            async with self._get_group_lock(str(gid)):
                img = await self.generate_report(bot, gid, silent=False)
            if img: yield event.image_result(img)
        except Exception as e:
            logger.error(f"Tool Error: {e}")

    # ================= 定时任务 =================
    async def run_scheduled_task(self):
        try:
            logger.info(f"[{VERSION}] 定时任务触发")
            bot = await self._get_bot()
            if not bot or not self.push_groups: return

            tasks = []
            for gid in self.push_groups:
                tasks.append(self._push_single_group(bot, str(gid)))
            await asyncio.gather(*tasks)
        except Exception:
            pass

    async def _push_single_group(self, bot, gid: str):
        async with self._push_semaphore:
            lock = self._get_group_lock(gid)
            if lock.locked(): return 

            async with lock:
                try:
                    img = await self.generate_report(bot, gid, silent=True)
                    if img:
                        cq = self._prepare_cq_code(img)
                        if cq:
                            await bot.api.call_action(API_SEND_GROUP_MSG, group_id=int(gid), message=cq)
                            logger.info(f"[{VERSION}] {gid} 推送成功")
                except Exception as e:
                    logger.error(f"[{VERSION}] {gid} 推送失败: {e}")
                await asyncio.sleep(PUSH_DELAY_BETWEEN_GROUPS)

    def _prepare_cq_code(self, img_path: str) -> Optional[str]:
        if img_path.startswith("http"):
            return f"[CQ:image,file={img_path}]"
        try:
            path_obj = Path(urllib.parse.urlparse(img_path).path).resolve()
            if not path_obj.exists(): return None
            if path_obj.stat().st_size > MAX_IMAGE_SIZE_BYTES: return None
            with open(path_obj, "rb") as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')
            return f"[CQ:image,file=base64://{b64}]"
        except Exception:
            return None
