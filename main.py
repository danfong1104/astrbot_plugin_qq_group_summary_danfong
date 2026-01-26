import json
import os
import re
import time
import datetime
import traceback
import asyncio
import base64
from collections import Counter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

def _parse_llm_json(text: str) -> dict:
    """增强型 JSON 解析器，支持清洗 Markdown 标记"""
    text = text.strip()
    # 去除 markdown 代码块
    if "```" in text:
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE | re.DOTALL).strip()
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    try:
        # 贪婪匹配最外层的 {}
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            json_str = match.group()
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    raise ValueError(f"无法提取有效 JSON，原始文本前50字: {text[:50]}...")

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.28")
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
        self.global_bot = None

        # 模板加载
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "report.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                self.html_template = f.read()
            logger.info(f"群聊总结(增强版): 模板加载成功 | v0.1.28 Stable")
        except FileNotFoundError:
            logger.error(f"群聊总结(增强版): 模板文件丢失: {template_path}")
            self.html_template = "<h1>Template Not Found</h1>"

        # 定时任务初始化
        self.scheduler = AsyncIOScheduler()
        if self.enable_auto_push:
            self.setup_schedule()

    def setup_schedule(self):
        """配置定时任务"""
        try:
            # 防止重复启动，先关闭旧的
            if self.scheduler.running:
                self.scheduler.shutdown()
            
            # 重新实例化调度器以确保干净状态
            self.scheduler = AsyncIOScheduler()
            
            hour, minute = self.push_time.split(":")
            trigger = CronTrigger(hour=int(hour), minute=int(minute))
            self.scheduler.add_job(self.run_scheduled_task, trigger)
            self.scheduler.start()
            logger.info(f"群聊总结(增强版): 定时任务已启动 -> 每天 {self.push_time}")
        except Exception as e:
            logger.error(f"群聊总结(增强版): 定时任务启动失败: {e}")

    def terminate(self):
        """【热重启优化】插件卸载/重载时的资源清理钩子"""
        try:
            if self.scheduler.running:
                self.scheduler.shutdown()
                logger.info("群聊总结(增强版): 定时任务已停止 (插件卸载/重载)")
        except Exception as e:
            logger.error(f"群聊总结(增强版): 资源清理失败: {e}")

    # ================= 核心修复：全兼容参数签名 =================
    # 使用 *args, **kwargs 接管所有可能的参数，解决 "必要参数缺失" 和 "TypeError"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot_instance(self, event: AstrMessageEvent, *args, **kwargs):
        """被动监听：自动捕获 Bot 实例"""
        if self.global_bot is None:
            self.global_bot = event.bot
            # logger.info("群聊总结(增强版): Bot 实例捕获成功 (被动监听)")

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def summarize_group(self, event: AstrMessageEvent, *args, **kwargs):
        """手动指令：/总结群聊"""
        # 手动触发时也强制刷新 Bot 实例，确保热重启后可用
        if self.global_bot is None:
            self.global_bot = event.bot
            
        group_id = event.get_group_id()
        yield event.plain_result(f"🌱 正在连接神经云端，回溯今日记忆...")
        
        # 调用生成逻辑 (silent=False 会输出错误提示给用户)
        img_result = await self.generate_report(event.bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，请检查后台日志。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        """LLM 工具调用"""
        if self.global_bot is None:
            self.global_bot = event.bot
            
        group_id = event.get_group_id()
        yield event.plain_result(f"🌱 正在分析今日群聊内容...")
        img_result = await self.generate_report(event.bot, group_id, silent=False)
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("无法生成总结。")
            
    # ================= 定时推送逻辑 =================

    async def run_scheduled_task(self):
        """执行定时推送"""
        try:
            logger.info("群聊总结(增强版): [Step 1] 开始定时推送...")
            
            if self.global_bot is None:
                logger.warning("群聊总结(增强版): [Warning] 未捕获 Bot 实例。")
                return

            bot = self.global_bot
            if not self.push_groups:
                logger.warning("群聊总结(增强版): 推送列表为空，请检查配置 push_groups。")
                return

            for group_id in self.push_groups:
                g_id_str = str(group_id)
                logger.info(f"群聊总结(增强版): 正在处理群 {g_id_str}")
                
                # 调用生成逻辑 (silent=True 不会给用户发错误文本)
                img_path = await self.generate_report(bot, g_id_str, silent=True)
                
                if img_path:
                    try:
                        cq_code = ""
                        # 情况1: 网络图片 URL
                        if img_path.startswith("http"):
                            cq_code = f"[CQ:image,file={img_path}]"
                        # 情况2: 本地图片 (转 Base64 以适应 Docker 等环境)
                        else:
                            local_path = img_path
                            if local_path.startswith("file://"):
                                local_path = local_path[7:]
                            # Windows 路径兼容 /C:/...
                            if os.name == 'nt' and local_path.startswith('/') and ':' in local_path:
                                local_path = local_path[1:]

                            if os.path.exists(local_path):
                                with open(local_path, "rb") as image_file:
                                    b64_str = base64.b64encode(image_file.read()).decode('utf-8')
                                cq_code = f"[CQ:image,file=base64://{b64_str}]"
                            else:
                                logger.error(f"群聊总结(增强版): 图片文件不存在: {local_path}")
                                continue

                        if cq_code:
                            await bot.api.call_action("send_group_msg", group_id=int(g_id_str), message=cq_code)
                            logger.info(f"群聊总结(增强版): 群 {g_id_str} 推送成功")
                            
                    except Exception as e:
                        logger.error(f"群聊总结(增强版): 群 {g_id_str} 推送异常: {e}")
                else:
                    logger.info(f"群聊总结(增强版): 群 {g_id_str} 无生成结果(可能无消息)")
                
                # 避免触发风控
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.error(f"群聊总结(增强版): 定时任务严重错误: {e}")
            logger.error(traceback.format_exc())

    # ================= 数据处理与生成 =================

    def get_today_start_timestamp(self):
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start.timestamp()

    async def fetch_group_history(self, bot, group_id: str, start_timestamp: float):
        all_messages = []
        message_seq = 0
        cutoff_time = start_timestamp

        for round_idx in range(self.max_query_rounds):
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
                
                batch_msgs = round_messages
                oldest_msg_time = batch_msgs[-1].get("time", 0)
                newest_msg_time = batch_msgs[0].get("time", 0)
                
                message_seq = round_messages[-1]["message_seq"]
                if oldest_msg_time > newest_msg_time:
                    message_seq = batch_msgs[0]["message_seq"]
                    oldest_msg_time = newest_msg_time
                
                all_messages.extend(batch_msgs)

                if oldest_msg_time < cutoff_time:
                    break
            except Exception as e:
                logger.error(f"群聊总结:Fetch loop error: {e}")
                break

        return all_messages

    def process_messages(self, messages: list, start_timestamp: float):
        cutoff_time = start_timestamp
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                continue

            raw_msg = msg.get("raw_message", "")
            if "[CQ:" in raw_msg and "image" in raw_msg: 
                pass
            
            sender = msg.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or "未知用户"
            
            if nickname in self.exclude_users:
                continue
            
            content = raw_msg

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
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in valid_msgs
        ])
        
        return valid_msgs, top_users, dict(trend_counter), chat_log

    async def generate_report(self, bot, group_id: str, silent: bool = False):
        today_start_ts = self.get_today_start_timestamp()
        
        try:
            group_info = await bot.api.call_action("get_group_info", group_id=group_id)
        except:
            group_info = {"group_name": "未知群聊"}

        raw_messages = await self.fetch_group_history(bot, group_id, start_timestamp=today_start_ts)
        if not raw_messages:
            if not silent: logger.warning(f"群 {group_id} 无法获取历史消息")
            return None

        valid_msgs, top_users, trend, chat_log = self.process_messages(raw_messages, start_timestamp=today_start_ts)
        if not valid_msgs:
            if not silent: logger.warning(f"群 {group_id} 今天无有效聊天记录")
            return None

        if len(chat_log) > self.msg_token_limit:
            chat_log = chat_log[:self.msg_token_limit]

        # 自定义提示词处理
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

        # ================= LLM 自动重试机制 (Max 3次) =================
        analysis_data = None
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        
        if not provider:
            logger.error("未配置 LLM Provider")
            return None

        for attempt in range(3):
            try:
                # 第一次不等待，后续重试等待 1秒
                if attempt > 0:
                    await asyncio.sleep(1)
                    logger.warning(f"群聊总结: LLM 解析失败，正在进行第 {attempt+1} 次重试...")

                response = await provider.text_chat(prompt, session_id=None)
                if not response or not response.completion_text:
                    continue
                    
                analysis_data = _parse_llm_json(response.completion_text)
                if analysis_data:
                    break # 成功拿到数据，跳出循环
            except Exception as e:
                logger.error(f"群聊总结: LLM Error (Attempt {attempt+1}): {e}")
        
        # 3次都失败后的兜底
        if not analysis_data:
            err_msg = "总结生成失败 (LLM 返回格式错误或连接超时)。"
            if not silent: logger.error(err_msg)
            return None
        # ============================================================

        try:
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
            logger.error(f"Render Error: {e}")
            return None
