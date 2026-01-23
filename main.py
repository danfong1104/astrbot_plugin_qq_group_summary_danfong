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

# 解析JSON
def _parse_llm_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            json_str = match.group()
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    raise ValueError("无法从 LLM 回复中提取有效的 JSON 数据")


@register("group_summary_danfong", "Danfong", "群聊总结增强版", "1.2.3")
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

        # 加载模板
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "report.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                self.html_template = f.read()
            logger.info(f"群聊总结(增强版): 成功加载模板: {template_path}")
        except FileNotFoundError:
            logger.error(f"群聊总结(增强版): 未找到模板文件: {template_path}")
            self.html_template = "<h1>Template Not Found</h1>"

        # 初始化定时器
        self.scheduler = AsyncIOScheduler()
        if self.enable_auto_push:
            self.setup_schedule()

    def setup_schedule(self):
        """设置定时任务"""
        try:
            hour, minute = self.push_time.split(":")
            trigger = CronTrigger(hour=int(hour), minute=int(minute))
            self.scheduler.add_job(self.run_scheduled_task, trigger)
            self.scheduler.start()
            logger.info(f"群聊总结(增强版): 定时任务已启动，将于每天 {self.push_time} 推送至 {self.push_groups}")
        except Exception as e:
            logger.error(f"群聊总结(增强版): 定时任务启动失败，请检查时间格式(HH:MM): {e}")

    async def run_scheduled_task(self):
        """定时任务执行逻辑"""
        logger.info("群聊总结(增强版): [Step 1] 开始执行定时推送任务...")
        
        # 1. 获取 Bot 实例
        bots = self.context.get_bots()
        if not bots:
            logger.warning("群聊总结(增强版): [Error] 未找到在线的 Bot 实例，任务终止。")
            return
        
        # 简单取第一个 Bot，通常就是你的 QQ 机器人
        bot_id = list(bots.keys())[0]
        bot = bots[bot_id]
        logger.info(f"群聊总结(增强版): [Step 2] 使用 Bot 实例: {bot_id}")
        
        if not self.push_groups:
            logger.warning("群聊总结(增强版): [Error] 推送列表(push_groups)为空，请在配置中添加群号。")
            return

        for group_id in self.push_groups:
            g_id_str = str(group_id)
            logger.info(f"群聊总结(增强版): [Step 3] 正在处理群: {g_id_str}")
            
            # --- 测试连接性 (可选，确认 Bot 能在群里说话) ---
            # await bot.api.call_action("send_group_msg", group_id=int(g_id_str), message=f"正在生成 {self.bot_name} 日报...")

            # 调用核心生成逻辑 (silent=True)
            img_path = await self.generate_report(bot, g_id_str, silent=True)
            logger.info(f"群聊总结(增强版): [Step 4] 图片生成路径: {img_path}")
            
            if img_path:
                # --- 路径清理逻辑 ---
                # 如果路径包含 file:// 前缀，Python 的 open() 无法直接读取，需要去掉
                local_path = img_path
                if local_path.startswith("file://"):
                    local_path = local_path[7:]
                # Windows 下 file:///C:/xxx 会变成 /C:/xxx，需要去掉开头的 /
                if os.name == 'nt' and local_path.startswith('/') and ':' in local_path:
                    local_path = local_path[1:]

                if os.path.exists(local_path):
                    try:
                        logger.info(f"群聊总结(增强版): [Step 5] 正在读取文件并转码: {local_path}")
                        with open(local_path, "rb") as image_file:
                            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        
                        # 使用 CQ 码发送，兼容性更好
                        cq_code = f"[CQ:image,file=base64://{encoded_string}]"
                        
                        logger.info(f"群聊总结(增强版): [Step 6] 正在调用 send_group_msg API...")
                        ret = await bot.api.call_action("send_group_msg", group_id=int(g_id_str), message=cq_code)
                        logger.info(f"群聊总结(增强版): [Success] 群 {g_id_str} 推送响应: {ret}")
                        
                    except Exception as e:
                        logger.error(f"群聊总结(增强版): [Error] 群 {g_id_str} 推送过程发生异常: {e}")
                        logger.error(traceback.format_exc())
                else:
                    logger.error(f"群聊总结(增强版): [Error] 找不到生成的图片文件: {local_path}")
            else:
                logger.info(f"群聊总结(增强版): [Skip] 群 {g_id_str} 生成返回为空(可能无消息)，跳过。")
            
            # 避免触发风控，暂停 5 秒
            await asyncio.sleep(5)

    def get_today_start_timestamp(self):
        """获取当天0点的时间戳"""
        now = datetime.datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start.timestamp()

    async def fetch_group_history(self, bot, group_id: str, start_timestamp: float):
        """分页获取群聊历史消息"""
        all_messages = []
        message_seq = 0
        cutoff_time = start_timestamp

        # logger.info(f"群聊总结:开始获取群 {group_id} 消息，截止时间戳: {cutoff_time}")

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
                
                # 更新 seq
                message_seq = round_messages[-1]["message_seq"]
                if oldest_msg_time > newest_msg_time:
                    message_seq = batch_msgs[0]["message_seq"]
                    oldest_msg_time = newest_msg_time
                
                all_messages.extend(batch_msgs)

                # 如果这批消息里最新的都已经比截止时间早了，或者最旧的碰到了截止线
                if oldest_msg_time < cutoff_time:
                    break
            except Exception as e:
                logger.error(f"群聊总结:Fetch loop error: {e}")
                break

        return all_messages

    def process_messages(self, messages: list, start_timestamp: float):
        """处理消息并进行黑名单过滤"""
        cutoff_time = start_timestamp
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                continue

            # 过滤系统消息
            raw_msg = msg.get("raw_message", "")
            if "[CQ:" in raw_msg and "image" in raw_msg: 
                pass
            
            sender = msg.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or "未知用户"
            
            # 黑名单过滤
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
        
        # 聊天记录按时间正序排列以便LLM理解
        valid_msgs.sort(key=lambda x: x['time'])
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in valid_msgs
        ])
        
        return valid_msgs, top_users, dict(trend_counter), chat_log

    async def generate_report(self, bot, group_id: str, silent: bool = False):
        """
        核心生成逻辑
        """
        # 1. 确定时间范围：今天0点到现在
        today_start_ts = self.get_today_start_timestamp()
        
        try:
            group_info = await bot.api.call_action("get_group_info", group_id=group_id)
        except:
            group_info = {"group_name": "未知群聊"}

        # 2. 获取消息
        raw_messages = await self.fetch_group_history(bot, group_id, start_timestamp=today_start_ts)
        if not raw_messages:
            if not silent: logger.warning(f"群 {group_id} 无法获取历史消息")
            return None

        # 3. 处理数据
        valid_msgs, top_users, trend, chat_log = self.process_messages(raw_messages, start_timestamp=today_start_ts)
        if not valid_msgs:
            if not silent: logger.warning(f"群 {group_id} 今天无有效聊天记录")
            return None

        if len(chat_log) > self.msg_token_limit:
            chat_log = chat_log[:self.msg_token_limit]

        # 4. LLM 请求
        prompt = f"""
        你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
        
        【要求】：
        1. 分析 3-8 个主要话题，每个话题包含：时间段（如 10:00 ~ 11:00）和简短内容。
        2. 写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。
        3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}
        
        【聊天记录】：
        {chat_log}
        """

        try:
            provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
            if not provider:
                logger.error("未配置 LLM Provider")
                return None

            response = await provider.text_chat(prompt, session_id=None)
            analysis_data = _parse_llm_json(response.completion_text)
        except Exception as e:
            logger.error(f"LLM Error: {e}")
            analysis_data = {"topics": [], "closing_remark": "总结生成失败，请检查后台日志。"}

        # 5. 渲染
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

    # --- 指令入口 (手动触发) ---
    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def summarize_group(self, event: AstrMessageEvent):
        """手动指令：/总结群聊 (默认总结今天)"""
        group_id = event.get_group_id()
        
        yield event.plain_result(f"🌱 正在回溯今日记忆...")
        
        # 手动调用，silent=False
        img_result = await self.generate_report(event.bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，可能是今天没有聊天记录或配置错误。")

    # --- Tool 入口 ---
    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent):
        """LLM调用工具：总结今天群聊"""
        group_id = event.get_group_id()
        yield event.plain_result(f"🌱 正在分析今日群聊内容...")
        
        img_result = await self.generate_report(event.bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("没有找到足够的聊天记录来生成总结。")
