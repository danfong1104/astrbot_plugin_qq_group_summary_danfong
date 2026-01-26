import json
import os
import re
import time
import datetime
import traceback
import asyncio
import base64
import textwrap
from pathlib import Path
from collections import Counter
from typing import List, Dict, Tuple, Optional, Any, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 全局常量配置 ---
VERSION = "0.1.31"
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0
PUSH_DELAY_BETWEEN_GROUPS = 5.0
# Base64 膨胀系数约 1.33，预留缓冲区。OneBot V11 普遍限制在 30MB 左右，保险起见设为 10MB
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024 
# 估算 1 Token ≈ 2 字符 (中文环境)
ESTIMATED_CHARS_PER_TOKEN = 2
LLM_TIMEOUT = 60 # LLM 请求超时时间 (秒)

def _parse_llm_json(text: str) -> dict:
    """
    鲁棒性 JSON 解析器：寻找最外层的 {} 对，忽略 Markdown 和杂音
    """
    text = text.strip()
    
    # 1. 简单清洗 Markdown 代码块标记
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()

    # 2. 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. 栈式寻找最外层大括号 (比正则更可靠)
    try:
        stack = 0
        start_index = -1
        end_index = -1
        
        for i, char in enumerate(text):
            if char == '{':
                if stack == 0:
                    start_index = i
                stack += 1
            elif char == '}':
                stack -= 1
                if stack == 0:
                    end_index = i + 1
                    # 找到第一个完整的 JSON 对象后停止 (通常是我们需要的)
                    break
        
        if start_index != -1 and end_index != -1:
            json_str = text[start_index:end_index]
            return json.loads(json_str)
    except Exception:
        pass

    raise ValueError(f"无法提取有效 JSON，文本前50字: {text[:50]}...")

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.31")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 基础配置
        self.max_msg_count = self.config.get("max_msg_count", 2000)
        self.max_query_rounds = self.config.get("max_query_rounds", 10)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.msg_token_limit = self.config.get("token_limit", 6000)
        
        # 新增配置
        self.exclude_users = self.config.get("exclude_users", []) # List[str]
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        self.summary_prompt_style = self.config.get("summary_prompt_style", "")
        
        # 状态管理
        self._global_bot = None
        self._bot_lock = asyncio.Lock()
        # 针对每个群的生成锁，防止手动和自动撞车
        self._group_locks: Dict[str, asyncio.Lock] = {}
        self.scheduler = None 

        # 模板加载
        self.template_path = Path(__file__).parent / "templates" / "report.html"
        self.html_template = self._load_template()

        # 启动定时任务
        if self.enable_auto_push:
            self.setup_schedule()

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        if group_id not in self._group_locks:
            self._group_locks[group_id] = asyncio.Lock()
        return self._group_locks[group_id]

    def _load_template(self) -> str:
        """加载 HTML 模板"""
        try:
            if not self.template_path.exists():
                raise FileNotFoundError(f"模板文件不存在: {self.template_path}")
            return self.template_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 模板加载失败: {e}")
            return "<h1>Template Load Error</h1>"

    def setup_schedule(self):
        """配置定时任务"""
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown()
            
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
        """插件卸载/重载时的资源清理"""
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)
                logger.info(f"群聊总结({VERSION}): 定时任务已停止")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 资源清理失败: {e}")

    # ================= HTML 渲染兼容层 =================
    async def html_render(self, template: str, data: dict, options: dict = None) -> Optional[str]:
        try:
            if hasattr(self.context, "image_renderer"):
                return await self.context.image_renderer.render(template, data, **(options or {}))
            logger.error(f"群聊总结({VERSION}): Context 缺少 image_renderer")
            return None
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 渲染失败: {e}")
            return None

    # ================= Bot 获取逻辑 (修复冷启动) =================
    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        # 1. 优先使用当前事件的 Bot
        if event and event.bot:
            async with self._bot_lock:
                self._global_bot = event.bot
            return event.bot

        # 2. 其次使用缓存
        if self._global_bot:
            return self._global_bot
        
        # 3. 冷启动兜底：主动从 Context 搜索
        try:
            if hasattr(self.context, "get_bots"):
                bots = self.context.get_bots()
                if bots:
                    # 优先寻找 OneBot 适配器
                    for bot_id, bot_inst in bots.items():
                        platform = getattr(bot_inst, "platform_name", "").lower()
                        if "qq" in platform or "onebot" in platform:
                            async with self._bot_lock:
                                self._global_bot = bot_inst
                            return bot_inst
                    # 没找到 OneBot，随便返回一个
                    return next(iter(bots.values()))
        except Exception:
            pass

        return None

    # ================= 事件监听 =================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot_instance(self, event: AstrMessageEvent, *args, **kwargs):
        if self._global_bot is None:
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
            yield event.plain_result("⚠️ 请在群聊中使用此指令。")
            return

        yield event.plain_result(f"🌱 正在连接神经云端，回溯今日记忆...")
        
        # 使用锁防止重复触发
        lock = self._get_group_lock(group_id)
        if lock.locked():
            yield event.plain_result("⚠️ 该群正在生成总结中，请稍候...")
            return

        async with lock:
            img_result = await self.generate_report(bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，请检查后台日志。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        bot = await self._get_bot(event)
        group_id = event.get_group_id()
        if not group_id or not bot:
            yield event.plain_result("无法生成总结。")
            return

        yield event.plain_result(f"🌱 正在分析今日群聊内容...")
        
        lock = self._get_group_lock(group_id)
        async with lock:
            img_result = await self.generate_report(bot, group_id, silent=False)
            
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("无法生成总结。")

    # ================= 核心逻辑 =================

    async def _fetch_messages(self, bot, group_id: str, start_timestamp: float) -> List[dict]:
        """获取群聊历史消息 (去重 + 协议检查)"""
        # 协议检查
        platform = getattr(bot, "platform_name", "").lower()
        if "telegram" in platform or "discord" in platform or "wechat" in platform:
            logger.warning(f"群聊总结({VERSION}): 平台 {platform} 可能不支持 get_group_msg_history")

        all_messages = []
        message_seq = 0
        cutoff_time = start_timestamp
        
        # 使用 Set 防止消息重复 (key: message_id)
        seen_msg_ids = set()

        for _ in range(self.max_query_rounds):
            if len(all_messages) >= self.max_msg_count:
                break

            try:
                params = {
                    "group_id": group_id,
                    "count": 200,
                    "message_seq": message_seq,
                    "reverseOrder": True,
                }
                resp: dict = await bot.api.call_action("get_group_msg_history", **params)
                round_messages = resp.get("messages", [])
                
                if not round_messages:
                    break
                
                # 统一按时间倒序排序 (Newest -> Oldest)
                batch_msgs = sorted(round_messages, key=lambda x: x.get('time', 0), reverse=True)
                
                # 更新游标 (取这批中最旧的一条的 seq)
                oldest_in_batch = batch_msgs[-1]
                current_min_seq = oldest_in_batch.get('message_seq')
                current_min_time = oldest_in_batch.get('time', 0)
                
                # 如果游标没有变化，说明到底了，防止死循环
                if message_seq != 0 and current_min_seq >= message_seq:
                    break
                message_seq = current_min_seq

                # 添加消息 (去重)
                for msg in batch_msgs:
                    msg_id = msg.get('message_id')
                    if msg_id and msg_id not in seen_msg_ids:
                        all_messages.append(msg)
                        seen_msg_ids.add(msg_id)

                if current_min_time <= cutoff_time:
                    break
                    
            except Exception as e:
                logger.error(f"群聊总结({VERSION}): 获取消息异常: {e}")
                break

        return all_messages

    def _process_messages(self, messages: List[dict], start_timestamp: float) -> Tuple[List[dict], List[dict], Dict[str, int], str]:
        """处理消息：过滤、统计、裁剪"""
        cutoff_time = start_timestamp
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                continue

            raw_msg = msg.get("raw_message", "")
            
            # 使用正则去除所有 CQ 码，只保留文本内容
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw_msg).strip()
            
            if not content:
                continue
            
            sender = msg.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or "未知用户"
            user_id = sender.get("user_id") # 尝试获取 UserID
            
            # 黑名单检查 (支持 昵称 和 UserID)
            if nickname in self.exclude_users:
                continue
            if user_id and str(user_id) in self.exclude_users:
                continue

            valid_msgs.append({
                "time": ts,
                "name": nickname,
                "content": content
            })
            user_counter[nickname] += 1
            
            # 修复时间格式 "0" -> "00"
            hour_str = datetime.datetime.fromtimestamp(ts).strftime("%H")
            trend_counter[hour_str] += 1

        top_users = [{"name": name, "count": count} for name, count in user_counter.most_common(5)]
        valid_msgs.sort(key=lambda x: x['time']) # 按时间正序排列
        
        # --- 修复：基于字符长度的裁剪逻辑 (而非条数) ---
        max_chars = self.msg_token_limit * ESTIMATED_CHARS_PER_TOKEN
        current_chars = 0
        final_msgs = []
        
        # 从最新的消息开始保留
        for msg in reversed(valid_msgs):
            # 估算单条长度: 名字 + 内容 + 时间戳开销
            msg_len = len(msg['content']) + len(msg['name']) + 10 
            if current_chars + msg_len > max_chars:
                break
            final_msgs.insert(0, msg)
            current_chars += msg_len

        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in final_msgs
        ])
        
        return valid_msgs, top_users, dict(trend_counter), chat_log

    def _construct_prompt(self, chat_log: str) -> str:
        user_style = self.config.get("summary_prompt_style") or \
                     f"写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。"
        
        if "{bot_name}" in user_style:
            user_style = user_style.replace("{bot_name}", self.bot_name)

        # 使用 textwrap 优化缩进，使用 XML 标签隔离数据防止注入
        return textwrap.dedent(f"""
            你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
            
            【要求】：
            1. 分析 3-8 个主要话题，每个话题包含：时间段（如 10:00 ~ 11:00）和简短内容。
            2. {user_style}
            3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}
            
            【聊天记录开始】：
            <chat_logs>
            {chat_log}
            </chat_logs>
            【聊天记录结束】
        """).strip()

    async def _call_llm(self, prompt: str) -> Optional[dict]:
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider:
            logger.error(f"群聊总结({VERSION}): 未配置 LLM Provider")
            return None

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                if attempt > 0:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
                    logger.warning(f"群聊总结({VERSION}): LLM 重试 {attempt+1}/{MAX_RETRY_ATTEMPTS}")

                # 增加超时控制
                response = await asyncio.wait_for(
                    provider.text_chat(prompt, session_id=None),
                    timeout=LLM_TIMEOUT
                )
                
                if not response or not response.completion_text:
                    continue
                    
                data = _parse_llm_json(response.completion_text)
                # 简单校验结构
                if isinstance(data, dict) and "topics" in data:
                    return data
            except asyncio.TimeoutError:
                logger.error(f"群聊总结({VERSION}): LLM 请求超时")
            except Exception as e:
                logger.error(f"群聊总结({VERSION}): LLM Error (Attempt {attempt+1}): {e}")
        
        return None

    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        try:
            today_start_ts = self.get_today_start_timestamp()
            
            try:
                group_info = await bot.api.call_action("get_group_info", group_id=group_id)
            except Exception:
                group_info = {"group_name": "未知群聊"}

            # 1. 获取
            raw_messages = await self._fetch_messages(bot, group_id, today_start_ts)
            if not raw_messages:
                if not silent: logger.warning(f"群聊总结({VERSION}): 群 {group_id} 无历史消息")
                return None

            # 2. 处理
            valid_msgs, top_users, trend, chat_log = self._process_messages(raw_messages, today_start_ts)
            if not valid_msgs:
                if not silent: logger.warning(f"群聊总结({VERSION}): 群 {group_id} 无有效记录")
                return None

            # 3. LLM
            prompt = self._construct_prompt(chat_log)
            analysis_data = await self._call_llm(prompt)
            if not analysis_data:
                # 兜底数据，防止渲染完全失败
                analysis_data = {
                    "topics": [{"time_range": "全天", "summary": "数据分析失败，但大家依然聊得很开心。"}], 
                    "closing_remark": "总结生成遇到了一点小障碍，请检查 LLM 设置。"
                }

            # 4. 渲染
            render_data = {
                "date": datetime.datetime.now().strftime("%Y.%m.%d"),
                "top_users": top_users,
                "trend": trend,
                "topics": analysis_data.get("topics", []),
                "summary_text": analysis_data.get("closing_remark", ""),
                "group_name": group_info.get("group_name", "群聊"),
                "bot_name": self.bot_name
            }
            options = {"quality": 95, "device_scale_factor_level": "ultra", "viewport_width": 500}
            
            return await self.html_render(self.html_template, render_data, options=options)

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 生成报告全局异常: {traceback.format_exc()}")
            return None

    async def run_scheduled_task(self):
        try:
            logger.info(f"群聊总结({VERSION}): [Step 1] 开始定时推送...")
            
            bot = await self._get_bot()
            if not bot:
                logger.warning(f"群聊总结({VERSION}): [Warning] 未捕获 Bot 实例，尝试主动获取...")
                # 再次尝试冷启动获取
                bot = await self._get_bot()
                if not bot:
                    logger.warning(f"群聊总结({VERSION}): 最终获取 Bot 失败，跳过。")
                    return

            if not self.push_groups:
                logger.warning(f"群聊总结({VERSION}): 推送列表为空。")
                return

            for group_id in self.push_groups:
                g_id_str = str(group_id)
                
                # 使用锁防止自动推送时用户手动触发导致的冲突
                lock = self._get_group_lock(g_id_str)
                if lock.locked():
                    logger.info(f"群聊总结({VERSION}): 群 {g_id_str} 正在进行任务，跳过定时推送")
                    continue

                async with lock:
                    logger.info(f"群聊总结({VERSION}): 正在处理群 {g_id_str}")
                    try:
                        img_path = await self.generate_report(bot, g_id_str, silent=True)
                        
                        if img_path:
                            cq_code = ""
                            if img_path.startswith("http"):
                                cq_code = f"[CQ:image,file={img_path}]"
                            else:
                                clean_path = str(Path(img_path))
                                if clean_path.startswith("file:"):
                                    clean_path = clean_path.replace("file:///", "").replace("file://", "")
                                
                                if os.path.exists(clean_path):
                                    # 检查文件大小防止超出协议限制
                                    f_size = os.path.getsize(clean_path)
                                    # Base64 约增大 33%
                                    if f_size * 1.35 > MAX_IMAGE_SIZE_BYTES:
                                        logger.error(f"图片过大 ({f_size} bytes)，跳过发送")
                                        continue
                                    
                                    try:
                                        with open(clean_path, "rb") as image_file:
                                            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                                        cq_code = f"[CQ:image,file=base64://{encoded_string}]"
                                    except Exception as file_err:
                                        logger.error(f"读取图片失败: {file_err}")
                                        continue
                                else:
                                    logger.error(f"群聊总结({VERSION}): 图片文件不存在: {clean_path}")

                            if cq_code:
                                # 强制转 int 确保兼容性
                                await bot.api.call_action("send_group_msg", group_id=int(g_id_str), message=cq_code)
                                logger.info(f"群聊总结({VERSION}): 群 {g_id_str} 推送成功")
                        else:
                            logger.info(f"群聊总结({VERSION}): 群 {g_id_str} 无生成内容")

                    except Exception as e:
                        logger.error(f"群聊总结({VERSION}): 群 {g_id_str} 推送异常: {e}")
                
                await asyncio.sleep(PUSH_DELAY_BETWEEN_GROUPS)
        
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 定时任务严重错误: {traceback.format_exc()}")

    def get_today_start_timestamp(self):
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start.timestamp()
