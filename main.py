import json
import os
import re
import datetime
import traceback
import asyncio
import base64
import urllib.parse
from pathlib import Path
from collections import Counter
from typing import List, Dict, Tuple, Optional, Any, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 全局常量配置 ---
VERSION = "0.1.32"
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0
PUSH_DELAY_BETWEEN_GROUPS = 5.0
MAX_IMAGE_SIZE_BYTES = 10_485_760  # 10MB
ESTIMATED_CHARS_PER_TOKEN = 2
LLM_TIMEOUT = 60
BASE64_CHUNK_SIZE = 8192 # 分块读取大小

# 平台常量
PLATFORM_ONEBOT = ("qq", "onebot", "aiocqhttp", "napcat", "llonebot")
PLATFORM_UNSUPPORTED = ("telegram", "discord", "wechat")

def _parse_llm_json(text: str) -> dict:
    """鲁棒性 JSON 解析器"""
    text = text.strip()
    # 简单清洗 Markdown
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 栈式寻找最外层大括号
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

    raise ValueError(f"无法提取有效 JSON，文本前50字: {text[:50]}...")

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.32")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 配置加载
        self.max_msg_count = self.config.get("max_msg_count", 2000)
        self.max_query_rounds = self.config.get("max_query_rounds", 10)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.msg_token_limit = self.config.get("token_limit", 6000)
        self.exclude_users = self.config.get("exclude_users", [])
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        self.summary_prompt_style = self.config.get("summary_prompt_style", "")
        
        # 状态管理
        self._global_bot = None
        self._bot_lock = asyncio.Lock()
        self._group_locks: Dict[str, asyncio.Lock] = {}
        self.scheduler = None 

        # 模板加载
        self.template_path = Path(__file__).parent / "templates" / "report.html"
        self.html_template = self._load_template()

        if self.enable_auto_push:
            self.setup_schedule()

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    def _load_template(self) -> str:
        try:
            if not self.template_path.exists():
                raise FileNotFoundError(f"模板文件不存在: {self.template_path}")
            return self.template_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 模板加载失败: {e}")
            return "<h1>Template Load Error</h1>"

    def setup_schedule(self):
        try:
            # 优雅关闭旧调度器 (wait=True 防止任务残留)
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=True)
            
            self.scheduler = AsyncIOScheduler()
            try:
                hour, minute = self.push_time.split(":")
                trigger = CronTrigger(hour=int(hour), minute=int(minute))
                self.scheduler.add_job(self.run_scheduled_task, trigger)
                self.scheduler.start()
                logger.info(f"群聊总结({VERSION}): 定时任务已启动 -> 每天 {self.push_time}")
            except ValueError:
                logger.error(f"群聊总结({VERSION}): 时间格式错误，应为 HH:MM")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 定时任务启动失败: {e}")

    def terminate(self):
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False) # 卸载时不等待
                logger.info(f"群聊总结({VERSION}): 定时任务已停止")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 资源清理失败: {e}")

    # ================= 核心修复：Bot获取 (读写锁保护) =================
    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        async with self._bot_lock:
            # 1. 优先更新缓存
            if event and event.bot:
                self._global_bot = event.bot
                return event.bot

            # 2. 读取缓存
            if self._global_bot:
                return self._global_bot
            
            # 3. 冷启动兜底
            try:
                if hasattr(self.context, "get_bots"):
                    bots = self.context.get_bots()
                    if bots:
                        for bot_inst in bots.values():
                            p_name = getattr(bot_inst, "platform_name", "").lower()
                            if any(k in p_name for k in PLATFORM_ONEBOT):
                                self._global_bot = bot_inst
                                return bot_inst
                        # 没找到 OneBot，返回第一个
                        self._global_bot = next(iter(bots.values()))
                        return self._global_bot
            except Exception:
                pass
            return None

    # ================= 事件监听 =================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot_instance(self, event: AstrMessageEvent, *args, **kwargs):
        # 双重检查锁优化性能
        if self._global_bot: return
        await self._get_bot(event)

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, *args, **kwargs):
        bot = await self._get_bot(event)
        if not bot:
            yield event.plain_result("❌ 无法获取 Bot 实例。")
            return
            
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return

        yield event.plain_result(f"🌱 正在连接神经云端，回溯今日记忆...")
        
        lock = self._get_group_lock(group_id)
        if lock.locked():
            yield event.plain_result("⚠️ 该群正在生成中，请稍候...")
            return

        async with lock:
            img_result = await self.generate_report(bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，请检查日志。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        bot = await self._get_bot(event)
        group_id = event.get_group_id()
        if not group_id or not bot:
            yield event.plain_result("无法生成总结。")
            return

        yield event.plain_result(f"🌱 正在分析群聊内容...")
        async with self._get_group_lock(group_id):
            img_result = await self.generate_report(bot, group_id, silent=False)
            
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("无法生成总结。")

    # ================= 核心逻辑拆分 (职责单一化) =================

    async def _fetch_messages(self, bot, group_id: str, start_ts: float) -> List[dict]:
        """原子方法：获取消息"""
        p_name = getattr(bot, "platform_name", "").lower()
        if any(k in p_name for k in PLATFORM_UNSUPPORTED):
            logger.warning(f"群聊总结({VERSION}): 平台 {p_name} 可能不支持历史消息API")

        all_msgs = []
        msg_seq = 0
        last_ids = set()

        for _ in range(self.max_query_rounds):
            if len(all_msgs) >= self.max_msg_count: break

            try:
                resp = await bot.api.call_action("get_group_msg_history", 
                    group_id=group_id, count=200, message_seq=msg_seq, reverseOrder=True)
                
                if not resp or "messages" not in resp: break
                
                batch = sorted(resp["messages"], key=lambda x: x.get('time', 0), reverse=True)
                if not batch: break

                oldest = batch[-1]
                msg_seq = oldest.get('message_seq')
                
                # 去重与边界检查
                valid_batch = []
                for m in batch:
                    mid = m.get('message_id')
                    if not mid:
                        logger.warning(f"群聊总结({VERSION}): 发现无 message_id 的消息，已跳过")
                        continue
                    if mid not in last_ids:
                        valid_batch.append(m)
                        last_ids.add(mid)
                
                all_msgs.extend(valid_batch)
                
                # 严格小于检查
                if oldest.get('time', 0) < start_ts:
                    break
                    
            except Exception as e:
                logger.error(f"群聊总结({VERSION}): API调用失败: {e}")
                break
                
        return all_msgs

    def _process_data(self, messages: List[dict], start_ts: float) -> Tuple[Any, Any, Any, str]:
        """原子方法：数据清洗统计"""
        valid_msgs = []
        u_count = Counter()
        t_count = Counter()
        
        for m in messages:
            if m.get('time', 0) < start_ts: continue
            
            raw = m.get('raw_message', "")
            # 正则清洗 CQ 码
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw).strip()
            if not content: continue
            
            sender = m.get('sender', {})
            nick = sender.get('card') or sender.get('nickname') or "未知"
            uid = sender.get('user_id')
            
            if nick in self.exclude_users or (uid and str(uid) in self.exclude_users):
                continue

            valid_msgs.append({"time": m['time'], "name": nick, "content": content})
            u_count[nick] += 1
            t_count[datetime.datetime.fromtimestamp(m['time']).strftime("%H")] += 1

        top_users = [{"name": k, "count": v} for k, v in u_count.most_common(5)]
        valid_msgs.sort(key=lambda x: x['time'])
        
        # 按条数截断 (估算)
        max_items = int(self.msg_token_limit / ESTIMATED_CHARS_PER_TOKEN)
        msgs_for_llm = valid_msgs[-max_items:] if len(valid_msgs) > max_items else valid_msgs
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in msgs_for_llm
        ])
        
        return valid_msgs, top_users, dict(t_count), chat_log

    async def _run_llm_analysis(self, chat_log: str) -> Optional[dict]:
        """原子方法：LLM 分析"""
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider: return None

        style = self.config.get("summary_prompt_style", "")
        if "{bot_name}" in style: style = style.replace("{bot_name}", self.bot_name)
        if not style: style = f"写一段{self.bot_name}的风格点评。"

        prompt = f"""
        角色：{self.bot_name}。任务：群聊总结。
        要求：
        1. 提取3-8个话题(时间段+摘要)。
        2. {style}
        3. 返回JSON：{{"topics": [{{"time_range":"", "summary":""}}], "closing_remark":""}}
        
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
                    if data: return data
            except Exception as e:
                logger.error(f"群聊总结({VERSION}): LLM第{i+1}次失败: {e}")
        return None

    # ================= 主入口重构 =================

    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        try:
            today_ts = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            
            # 1. 获取群名
            try:
                g_info = await bot.api.call_action("get_group_info", group_id=group_id)
            except:
                g_info = {"group_name": "未知群聊"}

            # 2. 拉取
            raw_msgs = await self._fetch_messages(bot, group_id, today_ts)
            if not raw_msgs:
                if not silent: logger.warning(f"群聊总结({VERSION}): 无历史消息")
                return None

            # 3. 处理
            _, top_users, trend, chat_log = self._process_data(raw_msgs, today_ts)
            if not chat_log:
                if not silent: logger.warning(f"群聊总结({VERSION}): 无有效文本消息")
                return None

            # 4. 分析
            analysis = await self._run_llm_analysis(chat_log)
            if not analysis:
                analysis = {"topics": [], "closing_remark": "分析失败，LLM 未响应。"}

            # 5. 渲染
            render_data = {
                "date": datetime.datetime.now().strftime("%Y.%m.%d"),
                "top_users": top_users,
                "trend": trend,
                "topics": analysis.get("topics", []),
                "summary_text": analysis.get("closing_remark", ""),
                "group_name": g_info.get("group_name", "群聊"),
                "bot_name": self.bot_name
            }
            
            # HTML渲染兼容层
            if hasattr(self.context, "image_renderer"):
                return await self.context.image_renderer.render(
                    self.html_template, render_data, 
                    quality=95, device_scale_factor_level="ultra", viewport_width=500
                )
            else:
                logger.error(f"群聊总结({VERSION}): 缺少渲染器")
                return None

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 生成流程异常: {traceback.format_exc()}")
            return None

    # ================= 定时任务 =================

    async def run_scheduled_task(self):
        try:
            logger.info(f"群聊总结({VERSION}): 定时任务触发")
            bot = await self._get_bot()
            if not bot:
                logger.warning(f"群聊总结({VERSION}): 无可用 Bot，跳过。")
                return

            if not self.push_groups: return

            for gid in self.push_groups:
                g_str = str(gid)
                
                # 并发锁检查
                lock = self._get_group_lock(g_str)
                if lock.locked(): continue

                async with lock:
                    try:
                        img = await self.generate_report(bot, g_str, silent=True)
                        if img:
                            cq = ""
                            if img.startswith("http"):
                                cq = f"[CQ:image,file={img}]"
                            else:
                                # 标准化路径处理
                                path_obj = Path(urllib.parse.urlparse(img).path)
                                # Windows 路径修正 (/C:/...)
                                if os.name == 'nt' and str(path_obj).startswith('\\') and ':' in str(path_obj):
                                    path_obj = Path(str(path_obj)[1:])
                                
                                if path_obj.exists():
                                    # 内存安全检查
                                    if path_obj.stat().st_size > MAX_IMAGE_SIZE_BYTES:
                                        logger.error(f"图片过大跳过: {path_obj}")
                                        continue
                                    
                                    # 内存安全读取 (分块虽然对 b64encode 意义不大，但符合规范)
                                    # 这里直接读入是为了 encoding，Python base64 暂不支持流式，但做了大小检查
                                    with open(path_obj, "rb") as f:
                                        b64 = base64.b64encode(f.read()).decode('utf-8')
                                    cq = f"[CQ:image,file=base64://{b64}]"
                            
                            if cq:
                                await bot.api.call_action("send_group_msg", group_id=int(g_str), message=cq)
                                logger.info(f"群聊总结({VERSION}): 群 {g_str} 推送成功")
                    except Exception as e:
                        logger.error(f"群聊总结({VERSION}): 群 {g_str} 推送失败: {e}")
                
                await asyncio.sleep(PUSH_DELAY_BETWEEN_GROUPS)

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 定时任务崩溃: {traceback.format_exc()}")
