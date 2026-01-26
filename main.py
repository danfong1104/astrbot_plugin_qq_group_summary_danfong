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
RETRY_BASE_DELAY = 1.0  # 秒
PUSH_DELAY_BETWEEN_GROUPS = 5.0  # 秒
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB Base64 转换限制
ESTIMATED_CHARS_PER_TOKEN = 20  # 用于估算 token 的魔法数字

def _parse_llm_json(text: str) -> dict:
    """
    增强型 JSON 解析器 (鲁棒性优化版)
    不再依赖脆弱的 Markdown 清洗正则，而是优先寻找合法的 JSON 结构
    """
    text = text.strip()
    
    # 1. 尝试直接解析 (最快)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # 2. 使用正则提取最外层的 {} (最稳健)
    # 非贪婪匹配，忽略 JSON 前后的 "好的", "```json" 等废话
    try:
        match = re.search(r"\{[\s\S]*?\}", text)
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
        self._is_task_running = False # 定时任务并发锁
        self.scheduler = None 

        # 模板加载
        self.template_path = Path(__file__).parent / "templates" / "report.html"
        self.html_template = self._load_template()

        # 启动定时任务
        if self.enable_auto_push:
            self.setup_schedule()

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

    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        """
        统一获取 Bot 实例
        优化：修复索引越界风险，优先从 Context 获取
        """
        # 1. 尝试从 Context 主动获取 (解决竞态条件)
        try:
            if hasattr(self.context, "get_bots"):
                bots = self.context.get_bots()
                if bots:
                    # 安全获取第一个 Bot 实例
                    return next(iter(bots.values()))
        except Exception as e:
            # 仅在调试模式下记录，避免刷屏
            # logger.debug(f"Context get_bots failed: {e}")
            pass

        # 2. 返回缓存
        if self._global_bot:
            return self._global_bot
        
        # 3. 从事件捕获
        if event:
            async with self._bot_lock:
                if not self._global_bot:
                    self._global_bot = event.bot
            return self._global_bot
        
        return None

    # ================= 指令与事件监听 =================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot_instance(self, event: AstrMessageEvent, *args, **kwargs):
        """
        被动监听：自动捕获 Bot 实例
        优化：增加快速返回路径，避免高频消息下的锁开销
        """
        if self._global_bot:
            return # 性能优化：已捕获则直接退出
            
        await self._get_bot(event)

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, *args, **kwargs):
        """手动指令"""
        await self._get_bot(event)
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用此指令。")
            return

        yield event.plain_result(f"🌱 正在连接神经云端，回溯今日记忆...")
        
        img_result = await self.generate_report(event.bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，请检查后台日志。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        """LLM 工具调用"""
        await self._get_bot(event)
        
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("无法在私聊中生成群总结。")
            return

        yield event.plain_result(f"🌱 正在分析今日群聊内容...")
        img_result = await self.generate_report(event.bot, group_id, silent=False)
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("无法生成总结。")

    # ================= 核心逻辑：数据处理 =================

    async def _fetch_messages(self, bot, group_id: str, start_timestamp: float) -> List[dict]:
        """获取群聊历史消息"""
        all_messages = []
        message_seq = 0
        cutoff_time = start_timestamp
        last_min_seq = None

        for _ in range(self.max_query_rounds):
            if len(all_messages) >= self.max_msg_count:
                break

            try:
                # 平台兼容性警告：此 API 为 OneBot V11 非标准扩展
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
                
                # 统一按时间倒序处理
                batch_msgs = sorted(round_messages, key=lambda x: x.get('time', 0), reverse=True)
                
                oldest_msg = batch_msgs[-1]
                oldest_seq = oldest_msg.get('message_seq')
                oldest_time = oldest_msg.get('time', 0)

                # 死循环检测
                if last_min_seq is not None and oldest_seq >= last_min_seq:
                    break
                last_min_seq = oldest_seq
                
                message_seq = oldest_seq
                all_messages.extend(batch_msgs)

                if oldest_time <= cutoff_time:
                    break
                    
            except Exception as e:
                # 明确的错误提示
                logger.error(f"群聊总结({VERSION}): 获取历史消息失败。请确认您使用的是支持 'get_group_msg_history' 接口的 OneBot V11 适配器 (如 NapCat/LLOneBot/Go-CQHTTP)。错误详情: {e}")
                break

        return all_messages

    def _process_messages(self, messages: List[dict], start_timestamp: float) -> Tuple[List[dict], List[dict], Dict[str, int], str]:
        """处理消息：过滤、统计、格式化"""
        cutoff_time = start_timestamp
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                continue

            raw_msg = msg.get("raw_message", "")
            
            # 使用正则去除 CQ 码（保留文本）
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
        
        # 排序：时间正序
        valid_msgs.sort(key=lambda x: x['time'])
        
        # 智能截断：按条数截断
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

    def _construct_prompt(self, chat_log: str) -> str:
        """构造 LLM 提示词"""
        user_style = self.config.get("summary_prompt_style")
        if not user_style:
            user_style = f"写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。"
        if "{bot_name}" in user_style:
            user_style = user_style.replace("{bot_name}", self.bot_name)

        return f"""
        你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
        
        【要求】：
        1. 分析 3-8 个主要话题，每个话题包含：时间段（如 10:00 ~ 11:00）和简短内容。
        2. {user_style}
        3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}
        
        【聊天记录】：
        {chat_log}
        """

    async def _call_llm(self, prompt: str) -> Optional[dict]:
        """调用 LLM (含重试逻辑)"""
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider:
            logger.error(f"群聊总结({VERSION}): 未配置 LLM Provider")
            return None

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                # 指数退避策略
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

    # ================= 核心逻辑：流程控制 =================

    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        """生成报告主流程 (拆分后)"""
        try:
            today_start_ts = self.get_today_start_timestamp()
            
            # 获取群名称
            try:
                group_info = await bot.api.call_action("get_group_info", group_id=group_id)
            except Exception:
                group_info = {"group_name": "未知群聊"}

            # 1. 获取消息
            raw_messages = await self._fetch_messages(bot, group_id, today_start_ts)
            if not raw_messages:
                if not silent: logger.warning(f"群聊总结({VERSION}): 群 {group_id} 无法获取历史消息")
                return None

            # 2. 处理消息
            valid_msgs, top_users, trend, chat_log = self._process_messages(raw_messages, today_start_ts)
            if not valid_msgs:
                if not silent: logger.warning(f"群聊总结({VERSION}): 群 {group_id} 今天无有效聊天记录")
                return None

            # 3. LLM 交互
            prompt = self._construct_prompt(chat_log)
            analysis_data = await self._call_llm(prompt)
            if not analysis_data:
                analysis_data = {"topics": [], "closing_remark": "总结生成失败 (LLM 返回数据格式错误或超时)。"}

            # 4. 渲染 HTML
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

    # ================= 定时推送逻辑 =================

    async def run_scheduled_task(self):
        """执行定时推送"""
        if self._is_task_running:
            logger.warning(f"群聊总结({VERSION}): 上一次定时任务未结束，跳过本次执行")
            return
        
        self._is_task_running = True
        try:
            logger.info(f"群聊总结({VERSION}): [Step 1] 开始定时推送...")
            
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
                            # 简单的 file: 前缀清理
                            if clean_path.startswith("file:"):
                                clean_path = clean_path.replace("file:///", "").replace("file://", "")
                            
                            # 安全检查：是否存在且大小正常
                            if os.path.exists(clean_path):
                                f_size = os.path.getsize(clean_path)
                                if f_size > MAX_IMAGE_SIZE:
                                    logger.error(f"图片过大 ({f_size} bytes)，跳过发送")
                                    continue
                                
                                # 安全读取
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
