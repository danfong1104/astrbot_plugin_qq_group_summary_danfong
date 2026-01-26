import json
import os
import re
import datetime
import traceback
import asyncio
import base64
import html  # 新增：用于HTML转义
import urllib.parse
from pathlib import Path
from collections import Counter
from typing import List, Dict, Tuple, Optional, Any, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

# --- 全局常量配置 ---
VERSION = "0.1.30"

# API Action 常量
API_GET_GROUP_MSG_HISTORY = "get_group_msg_history"
API_GET_GROUP_INFO = "get_group_info"
API_SEND_GROUP_MSG = "send_group_msg"

# 逻辑常量
MAX_RETRY_ATTEMPTS = 3
LLM_TIMEOUT = 45  # 缩短超时，避免长阻塞
RETRY_BASE_DELAY = 2.0
MAX_CONCURRENT_PUSH = 3  # 批量推送时的并发限制
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ESTIMATED_CHARS_PER_TOKEN = 2  # 估算 1 Token ≈ 2 中文字符

# 平台识别
PLATFORM_ONEBOT = ("qq", "onebot", "aiocqhttp", "napcat", "llonebot")
PLATFORM_UNSUPPORTED = ("telegram", "discord", "wechat")

def _parse_llm_json(text: str) -> dict:
    """鲁棒性 JSON 解析器：寻找最外层 {}，忽略干扰文本"""
    text = text.strip()
    # 移除 Markdown 代码块
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 栈式提取
    try:
        stack = 0
        start = -1
        for i, char in enumerate(text):
            if char == '{':
                if stack == 0: start = i
                stack += 1
            elif char == '}':
                stack -= 1
                if stack == 0:
                    return json.loads(text[start:i+1])
    except Exception:
        pass

    raise ValueError(f"JSON 解析失败，内容片段: {text[:50]}...")

class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 配置加载 (一次性读取)
        self.max_msg_count = self.config.get("max_msg_count", 2000)
        self.max_query_rounds = self.config.get("max_query_rounds", 10)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.msg_token_limit = self.config.get("token_limit", 6000)
        self.exclude_users = set(self.config.get("exclude_users", [])) # 转为set加速查找
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        
        # 处理提示词模板
        raw_style = self.config.get("summary_prompt_style", "")
        self.prompt_style = raw_style.replace("{bot_name}", self.bot_name) if raw_style else \
                            f"写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。"
        
        # 状态管理
        self._global_bot = None
        self._bot_lock = asyncio.Lock()
        self._group_locks: Dict[str, asyncio.Lock] = {}
        self.scheduler = None 
        self._push_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PUSH) # 并发控制

        # 模板加载
        self.template_path = Path(__file__).parent / "templates" / "report.html"
        self.html_template = self._load_template()

        # 启动调度
        if self.enable_auto_push:
            self.setup_schedule()

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    def _load_template(self) -> str:
        try:
            if not self.template_path.exists():
                raise FileNotFoundError(f"Missing template: {self.template_path}")
            return self.template_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 模板加载失败: {e}")
            return "<h1>Template Load Error</h1>"

    def setup_schedule(self):
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=True) # 等待旧任务清理
            
            self.scheduler = AsyncIOScheduler()
            try:
                hour, minute = self.push_time.split(":")
                trigger = CronTrigger(hour=int(hour), minute=int(minute))
                self.scheduler.add_job(self.run_scheduled_task, trigger)
                self.scheduler.start()
                logger.info(f"群聊总结({VERSION}): 定时任务已启动 -> {self.push_time}")
            except ValueError:
                logger.error(f"群聊总结({VERSION}): 时间格式错误，应为 HH:MM")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 调度器启动失败: {e}")

    def terminate(self):
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)
                logger.info(f"群聊总结({VERSION}): 定时任务已停止")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 资源清理失败: {e}")

    # ================= 核心：Bot 获取 (双重保障) =================
    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        async with self._bot_lock:
            if event and event.bot:
                self._global_bot = event.bot
                return event.bot

            if self._global_bot:
                return self._global_bot
            
            # 冷启动兜底：遍历 Context 寻找 OneBot
            try:
                if hasattr(self.context, "get_bots"):
                    bots = self.context.get_bots()
                    if bots:
                        for bot_inst in bots.values():
                            p_name = getattr(bot_inst, "platform_name", "").lower()
                            if any(k in p_name for k in PLATFORM_ONEBOT):
                                self._global_bot = bot_inst
                                return bot_inst
                        # 无 OneBot，返回任意一个
                        self._global_bot = next(iter(bots.values()))
                        return self._global_bot
            except Exception:
                pass
            return None

    # ================= HTML 渲染兼容层 =================
    async def html_render(self, template: str, data: dict, options: dict = None) -> Optional[str]:
        try:
            if hasattr(self.context, "image_renderer"):
                return await self.context.image_renderer.render(template, data, **(options or {}))
            return None
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 渲染异常: {e}")
            return None

    # ================= 消息处理流水线 (Atomic Methods) =================

    async def _fetch_messages(self, bot, group_id: str, start_ts: float) -> List[dict]:
        """拉取历史消息：含协议检查、去重、死循环熔断"""
        p_name = getattr(bot, "platform_name", "").lower()
        if any(k in p_name for k in PLATFORM_UNSUPPORTED):
            logger.warning(f"群聊总结({VERSION}): 平台 {p_name} 可能不支持 {API_GET_GROUP_MSG_HISTORY}")

        all_msgs = []
        msg_seq = 0
        last_ids = set()
        
        # 熔断机制：记录上一次的最旧 seq
        last_min_seq = None 

        for _ in range(self.max_query_rounds):
            if len(all_msgs) >= self.max_msg_count: break

            try:
                resp = await bot.api.call_action(API_GET_GROUP_MSG_HISTORY, 
                    group_id=group_id, count=200, message_seq=msg_seq, reverseOrder=True)
                
                if not resp or "messages" not in resp: break
                
                # 统一倒序 (Newest -> Oldest)
                batch = sorted(resp["messages"], key=lambda x: x.get('time', 0), reverse=True)
                if not batch: break

                oldest = batch[-1]
                current_seq = oldest.get('message_seq')
                
                # 死循环熔断：如果 seq 没变或为 None，停止
                if current_seq is None or (last_min_seq is not None and current_seq >= last_min_seq):
                    break
                last_min_seq = current_seq
                msg_seq = current_seq

                # 收集有效消息 (去重)
                for m in batch:
                    mid = m.get('message_id')
                    if not mid: continue # 跳过无 ID 消息
                    
                    if mid not in last_ids:
                        all_msgs.append(m)
                        last_ids.add(mid)
                
                # 边界检查 (严格小于)
                if oldest.get('time', 0) < start_ts:
                    break
                    
            except Exception as e:
                # 仅记录非预期错误
                if "ActionFailed" not in str(e):
                    logger.error(f"群聊总结({VERSION}): 拉取消息错误: {e}")
                break
                
        return all_msgs

    def _process_data(self, messages: List[dict], start_ts: float) -> Tuple[List[dict], List[dict], Dict[str, int], str]:
        """数据清洗与统计：支持 Token 截断、UID 统计"""
        valid_msgs = []
        # 使用 UID 统计更准确，如果没有 UID 则回退到 Nickname
        user_stats = Counter() 
        trend_stats = Counter()
        
        # 预计算截断阈值 (字符数)
        char_limit = self.msg_token_limit * ESTIMATED_CHARS_PER_TOKEN
        
        for m in messages:
            if m.get('time', 0) < start_ts: continue
            
            raw = m.get('raw_message', "")
            # 正则清洗 CQ 码
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw).strip()
            if not content: continue
            
            sender = m.get('sender', {})
            nick = sender.get('card') or sender.get('nickname') or "未知用户"
            uid = str(sender.get('user_id', ''))
            
            # 黑名单 (同时检查 nick 和 uid)
            if nick in self.exclude_users or uid in self.exclude_users:
                continue

            # 统计 key 优先用 nick (用于展示)，实际逻辑可优化为 uid map nick
            # 这里为了简单展示，直接统计 nick
            user_stats[nick] += 1
            
            # 趋势统计 (补齐两位 00-23)
            hour = datetime.datetime.fromtimestamp(m['time']).strftime("%H")
            trend_stats[hour] += 1

            valid_msgs.append({
                "time": m['time'],
                "name": nick,
                "content": content
            })

        top_users = [{"name": k, "count": v} for k, v in user_stats.most_common(5)]
        
        # 排序：按时间正序，准备生成 ChatLog
        valid_msgs.sort(key=lambda x: x['time'])
        
        # --- 真实字符截断逻辑 ---
        # 从最新的消息开始累加，直到超过 char_limit
        accumulated_chars = 0
        final_msgs = []
        for msg in reversed(valid_msgs):
            # 估算单条开销: 内容 + 名字 + 时间戳 + 换行
            cost = len(msg['content']) + len(msg['name']) + 15 
            if accumulated_chars + cost > char_limit:
                break
            final_msgs.insert(0, msg) # 保持正序插入
            accumulated_chars += cost
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in final_msgs
        ])
        
        return valid_msgs, top_users, dict(trend_stats), chat_log

    async def _run_llm(self, chat_log: str) -> Optional[dict]:
        """LLM 调用与重试"""
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider: return None

        prompt = f"""
        角色：{self.bot_name}。任务：群聊总结。
        要求：
        1. 提取3-8个话题(时间段+摘要)。
        2. {self.prompt_style}
        3. 严禁包含Markdown代码块标记，直接返回JSON对象。
        格式：{{"topics": [{{"time_range":"", "summary":""}}], "closing_remark":""}}
        
        记录：
        {chat_log}
        """

        for i in range(MAX_RETRY_ATTEMPTS):
            try:
                if i > 0: await asyncio.sleep(RETRY_BASE_DELAY * (2 ** i))
                
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt, session_id=None), 
                    timeout=LLM_TIMEOUT
                )
                if resp and resp.completion_text:
                    data = _parse_llm_json(resp.completion_text)
                    # 简单校验
                    if isinstance(data, dict) and ("topics" in data or "closing_remark" in data):
                        return data
            except Exception as e:
                logger.error(f"群聊总结({VERSION}): LLM第{i+1}次异常: {e}")
        return None

    # ================= 流程总控 =================

    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        try:
            # 1. 初始化
            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_ts = today.timestamp()
            
            # 2. 获取群名 (API 类型兼容：get_group_info 接受 str)
            try:
                g_info = await bot.api.call_action(API_GET_GROUP_INFO, group_id=str(group_id))
            except:
                g_info = {"group_name": "群聊"}

            # 3. 拉取消息
            raw_msgs = await self._fetch_messages(bot, str(group_id), today_ts)
            if not raw_msgs:
                if not silent: logger.warning(f"群聊总结({VERSION}): {group_id} 无新消息")
                return None

            # 4. 数据处理
            _, top_users, trend, chat_log = self._process_data(raw_msgs, today_ts)
            if not chat_log:
                if not silent: logger.warning(f"群聊总结({VERSION}): {group_id} 无有效文本")
                return None

            # 5. LLM 分析
            analysis = await self._run_llm(chat_log)
            if not analysis:
                analysis = {"topics": [], "closing_remark": "分析超时，生成失败。"}

            # 6. 安全渲染 (HTML转义)
            safe_group_name = html.escape(g_info.get("group_name", "群聊"))
            safe_topics = [
                {"time_range": html.escape(str(t.get("time_range",""))), "summary": html.escape(str(t.get("summary","")))} 
                for t in analysis.get("topics", [])
            ]
            safe_remark = html.escape(str(analysis.get("closing_remark", "")))

            render_data = {
                "date": today.strftime("%Y.%m.%d"),
                "top_users": top_users, # 内部已清洗
                "trend": trend,
                "topics": safe_topics,
                "summary_text": safe_remark,
                "group_name": safe_group_name,
                "bot_name": self.bot_name
            }
            
            return await self.html_render(
                self.html_template, render_data, 
                options={"quality": 95, "viewport_width": 500}
            )

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 流程崩溃: {traceback.format_exc()}")
            return None

    # ================= 任务接口 =================

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, *args, **kwargs):
        """手动触发"""
        bot = await self._get_bot(event)
        if not bot:
            yield event.plain_result("❌ 无法获取 Bot")
            return
            
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("⚠️ 请在群内使用")
            return

        yield event.plain_result(f"🌱 正在生成今日总结...")
        
        lock = self._get_group_lock(str(gid))
        if lock.locked():
            yield event.plain_result("⚠️ 任务进行中...")
            return

        async with lock:
            img = await self.generate_report(bot, gid, silent=False)
        
        if img:
            yield event.image_result(img)
        else:
            yield event.plain_result("❌ 生成失败")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        """LLM 工具触发"""
        bot = await self._get_bot(event)
        gid = event.get_group_id()
        if not gid or not bot:
            yield event.plain_result("无法执行")
            return

        yield event.plain_result(f"🌱 正在分析...")
        async with self._get_group_lock(str(gid)):
            img = await self.generate_report(bot, gid, silent=False)
            
        if img:
            yield event.image_result(img)
        else:
            yield event.plain_result("失败")

    # ================= 定时任务 (并发控制) =================

    async def run_scheduled_task(self):
        try:
            logger.info(f"群聊总结({VERSION}): 定时任务触发")
            bot = await self._get_bot()
            if not bot:
                logger.warning(f"群聊总结({VERSION}): 无 Bot 实例")
                return

            if not self.push_groups: return

            # 使用信号量控制并发，防止瞬间并发过高
            tasks = []
            for gid in self.push_groups:
                tasks.append(self._push_single_group(bot, str(gid)))
            
            await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 定时任务崩溃: {traceback.format_exc()}")

    async def _push_single_group(self, bot, gid: str):
        """单个群推送逻辑 (受信号量控制)"""
        async with self._push_semaphore:
            lock = self._get_group_lock(gid)
            if lock.locked(): return # 跳过正在手动执行的群

            async with lock:
                try:
                    logger.info(f"群聊总结({VERSION}): 正在处理 {gid}")
                    img = await self.generate_report(bot, gid, silent=True)
                    
                    if img:
                        cq = self._prepare_cq_code(img)
                        if cq:
                            # 强制转 int 以兼容 send_group_msg
                            await bot.api.call_action(API_SEND_GROUP_MSG, group_id=int(gid), message=cq)
                            logger.info(f"群聊总结({VERSION}): {gid} 推送成功")
                except Exception as e:
                    logger.error(f"群聊总结({VERSION}): {gid} 推送失败: {e}")
                
                # 任务间微小间隔
                await asyncio.sleep(1)

    def _prepare_cq_code(self, img_path: str) -> Optional[str]:
        """构建图片 CQ 码，处理路径与 Base64"""
        if img_path.startswith("http"):
            return f"[CQ:image,file={img_path}]"
        
        try:
            path_obj = Path(urllib.parse.urlparse(img_path).path)
            # Windows 兼容
            if os.name == 'nt' and str(path_obj).startswith('\\') and ':' in str(path_obj):
                path_obj = Path(str(path_obj)[1:])
            
            if not path_obj.exists():
                logger.error(f"图片丢失: {path_obj}")
                return None

            if path_obj.stat().st_size > MAX_IMAGE_SIZE_BYTES:
                logger.error(f"图片过大跳过")
                return None

            # 内存安全读取
            with open(path_obj, "rb") as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')
            return f"[CQ:image,file=base64://{b64}]"
        except Exception as e:
            logger.error(f"图片处理失败: {e}")
            return None
