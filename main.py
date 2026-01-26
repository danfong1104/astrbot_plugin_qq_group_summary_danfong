import json
import os
import re
import datetime
import traceback
import asyncio
import base64
from pathlib import Path
from collections import Counter
from typing import List, Dict, Tuple, Optional, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

# --- 全局常量配置 ---
VERSION = "0.1.30"
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
PUSH_DELAY_BETWEEN_GROUPS = 5.0
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ESTIMATED_CHARS_PER_TOKEN = 2  # 中文环境调整为2
MAX_JSON_PARSE_LENGTH = 50000  # 限制正则处理的最大字符数

def _parse_llm_json(text: str) -> dict:
    """增强型 JSON 解析器 (带长度限制防ReDoS)"""
    # 截取头部以防止超长文本导致的正则卡死
    process_text = text[:MAX_JSON_PARSE_LENGTH].strip()
    
    if "```" in process_text:
        process_text = re.sub(r"^```(json)?|```$", "", process_text, flags=re.MULTILINE | re.DOTALL).strip()
    
    try:
        return json.loads(process_text)
    except json.JSONDecodeError:
        pass
    
    try:
        # 非贪婪匹配
        match = re.search(r"\{[\s\S]*?\}", process_text)
        if match:
            json_str = match.group()
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    raise ValueError(f"无法提取有效 JSON，文本前50字: {text[:50]}...")

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
        self.exclude_users = self.config.get("exclude_users", [])
        self.enable_auto_push = self.config.get("enable_auto_push", False)
        self.push_time = self.config.get("push_time", "23:00")
        self.push_groups = self.config.get("push_groups", [])
        self.summary_prompt_style = self.config.get("summary_prompt_style", "")
        
        # 状态管理
        self._global_bot = None
        self._bot_lock = asyncio.Lock()
        self._is_task_running = False
        self.scheduler = None 

        # 模板加载
        self.template_path = Path(__file__).parent / "templates" / "report.html"
        self.html_template = self._load_template()

        # 启动定时任务 (不在 __init__ 中直接 start，而是 setup)
        if self.enable_auto_push:
            self.setup_schedule()

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
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)
                logger.info(f"群聊总结({VERSION}): 定时任务已停止")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 资源清理失败: {e}")

    # ================= 核心修复：HTML 渲染兼容层 =================
    async def html_render(self, template: str, data: dict, options: dict = None) -> Optional[str]:
        """
        兼容 Star 基类缺失 html_render 的问题。
        尝试调用 Context 中的 image_renderer。
        """
        try:
            # 1. 尝试使用 AstrBot 内置的渲染器
            if hasattr(self.context, "image_renderer"):
                # 这里假设 renderer 支持 render_html 方法，具体 API 视 AstrBot 版本而定
                # 大多数情况下是 render(html_str) 或类似
                return await self.context.image_renderer.render(template, data, **(options or {}))
            
            # 2. 如果没有，记录错误 (此处可以扩展其他渲染逻辑)
            logger.error(f"群聊总结({VERSION}): 当前 Context 不支持 HTML 渲染 (缺少 image_renderer)")
            return None
        except Exception as e:
            # 兼容性兜底：如果是旧版 AstrBot，可能 render 签名不同
            try:
                if hasattr(self.context, "render_template"):
                     return await self.context.render_template(template, **data)
            except Exception:
                pass
            
            logger.error(f"群聊总结({VERSION}): 渲染失败: {e}")
            return None

    # ================= 核心修复：Bot 获取逻辑 =================
    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        """
        统一获取 Bot 实例。
        优先级: Event.bot (当前交互) > Cache (之前交互) > Context (兜底)
        """
        # 1. 优先使用当前事件的 Bot (最准确)
        if event and event.bot:
            async with self._bot_lock:
                self._global_bot = event.bot
            return event.bot

        # 2. 其次使用缓存的 Bot
        if self._global_bot:
            return self._global_bot
        
        # 3. 最后尝试从 Context 获取 (兜底，可能拿到错误的 Bot)
        try:
            if hasattr(self.context, "get_bots"):
                bots = self.context.get_bots()
                if bots:
                    # 记录警告，因为这可能是随机选的一个 Bot
                    logger.warning(f"群聊总结({VERSION}): 使用 Context 默认 Bot，可能与目标群不匹配。")
                    return next(iter(bots.values()))
        except Exception:
            pass

        return None

    # ================= 指令与事件监听 =================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot_instance(self, event: AstrMessageEvent, *args, **kwargs):
        """被动监听：只在缓存为空时加锁更新"""
        if self._global_bot is None:
            await self._get_bot(event)

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, *args, **kwargs):
        """手动指令"""
        bot = await self._get_bot(event)
        if not bot:
            yield event.plain_result("❌ 无法获取 Bot 实例。")
            return
            
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用此指令。")
            return

        yield event.plain_result(f"🌱 正在连接神经云端，回溯今日记忆...")
        img_result = await self.generate_report(bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，请检查后台日志。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        """LLM 工具调用"""
        bot = await self._get_bot(event)
        group_id = event.get_group_id()
        
        if not group_id or not bot:
            yield event.plain_result("无法生成总结。")
            return

        yield event.plain_result(f"🌱 正在分析今日群聊内容...")
        img_result = await self.generate_report(bot, group_id, silent=False)
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("无法生成总结。")

    # ================= 数据处理 =================

    async def _fetch_messages(self, bot, group_id: str, start_timestamp: float) -> List[dict]:
        """获取群聊历史消息"""
        # --- 核心修复：协议检查 ---
        adapter_name = getattr(bot, "platform_name", "").lower()
        # 宽泛检查，允许 qq, aiocqhttp, onebot 等关键字
        if "telegram" in adapter_name or "discord" in adapter_name or "wechat" in adapter_name:
            logger.warning(f"群聊总结({VERSION}): 当前适配器 {adapter_name} 可能不支持 get_group_msg_history API")
            # 不直接 return，尝试运行以防万一
        # ------------------------

        all_messages = []
        message_seq = 0
        cutoff_time = start_timestamp
        last_min_seq = None

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
                
                batch_msgs = sorted(round_messages, key=lambda x: x.get('time', 0), reverse=True)
                
                oldest_msg = batch_msgs[-1]
                oldest_seq = oldest_msg.get('message_seq')
                oldest_time = oldest_msg.get('time', 0)

                if last_min_seq is not None and oldest_seq >= last_min_seq:
                    break
                last_min_seq = oldest_seq
                
                message_seq = oldest_seq
                all_messages.extend(batch_msgs)

                if oldest_time <= cutoff_time:
                    break
                    
            except Exception as e:
                logger.error(f"群聊总结({VERSION}): 获取消息异常 (协议可能不兼容): {e}")
                break

        return all_messages

    def _process_messages(self, messages: List[dict], start_timestamp: float) -> Tuple[List[dict], List[dict], Dict[str, int], str]:
        cutoff_time = start_timestamp
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                continue

            raw_msg = msg.get("raw_message", "")
            
            # 使用正则去除 CQ 码，保留文本
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw_msg).strip()
            
            if not content:
                continue
            
            sender = msg.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or "未知用户"
            
            if nickname in self.exclude_users:
                continue

            valid_msgs.append({
                "time": ts,
                "name": nickname,
                "content": content
            })
            user_counter[nickname] += 1
            
            hour_str = datetime.datetime.fromtimestamp(ts).strftime("%H")
            trend_counter[str(int(hour_str))] += 1

        top_users = [{"name": name, "count": count} for name, count in user_counter.most_common(5)]
        valid_msgs.sort(key=lambda x: x['time'])
        
        # 智能截断 (使用更新后的常量)
        max_items = int(self.msg_token_limit / ESTIMATED_CHARS_PER_TOKEN)
        if len(valid_msgs) > max_items:
             valid_msgs_for_llm = valid_msgs[-max_items:]
        else:
             valid_msgs_for_llm = valid_msgs

        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in valid_msgs_for_llm
        ])
        
        return valid_msgs, top_users, dict(trend_counter), chat_log

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

                response = await provider.text_chat(prompt, session_id=None)
                if not response or not response.completion_text:
                    continue
                    
                data = _parse_llm_json(response.completion_text)
                if data:
                    return data
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

            raw_messages = await self._fetch_messages(bot, group_id, today_start_ts)
            if not raw_messages:
                if not silent: logger.warning(f"群聊总结({VERSION}): 群 {group_id} 无法获取历史消息")
                return None

            valid_msgs, top_users, trend, chat_log = self._process_messages(raw_messages, today_start_ts)
            if not valid_msgs:
                if not silent: logger.warning(f"群聊总结({VERSION}): 群 {group_id} 今天无有效聊天记录")
                return None

            user_style = self.config.get("summary_prompt_style")
            if not user_style:
                user_style = f"写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。"
            if "{bot_name}" in user_style:
                user_style = user_style.replace("{bot_name}", self.bot_name)

            prompt = f"""
            你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
            
            【要求】：
            1. 分析 3-8 个主要话题，每个话题包含：时间段（如 10:00 ~ 11:00）和简短内容。
            2. {user_style}
            3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}
            
            【聊天记录】：
            {chat_log}
            """

            analysis_data = await self._call_llm(prompt)
            if not analysis_data:
                analysis_data = {"topics": [], "closing_remark": "总结生成失败 (LLM 返回数据格式错误或超时)。"}

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
            
            # 使用兼容层 html_render
            return await self.html_render(self.html_template, render_data, options=options)

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 生成报告全局异常: {traceback.format_exc()}")
            return None

    async def run_scheduled_task(self):
        if self._is_task_running:
            logger.warning(f"群聊总结({VERSION}): 上一次定时任务未结束，跳过本次执行")
            return
        
        self._is_task_running = True
        try:
            logger.info(f"群聊总结({VERSION}): [Step 1] 开始定时推送...")
            
            # 使用 _get_bot 获取 Bot 实例 (优先使用活跃的)
            bot = await self._get_bot()
            if not bot:
                logger.warning(f"群聊总结({VERSION}): [Warning] 未捕获 Bot 实例，跳过推送。")
                return

            if not self.push_groups:
                logger.warning(f"群聊总结({VERSION}): 推送列表为空。")
                return

            for group_id in self.push_groups:
                g_id_str = str(group_id)
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
                                f_size = os.path.getsize(clean_path)
                                if f_size > MAX_IMAGE_SIZE:
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
                            await bot.api.call_action("send_group_msg", group_id=int(g_id_str), message=cq_code)
                            logger.info(f"群聊总结({VERSION}): 群 {g_id_str} 推送成功")
                    else:
                        logger.info(f"群聊总结({VERSION}): 群 {g_id_str} 无内容")

                except Exception as e:
                    logger.error(f"群聊总结({VERSION}): 群 {g_id_str} 推送异常: {e}")
                
                await asyncio.sleep(PUSH_DELAY_BETWEEN_GROUPS)
        
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 定时任务严重错误: {traceback.format_exc()}")
        finally:
            self._is_task_running = False

    def get_today_start_timestamp(self):
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start.timestamp()
