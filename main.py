import json
import os
import re
import time
import datetime
import traceback
import asyncio
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


@register("group_summary_danfong", "Danfong", "群聊总结增强版", "1.2.0")
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
        logger.info("群聊总结(增强版): 开始执行定时推送任务...")
        
        # 获取一个可用的 Bot 实例 (通常取第一个)
        bots = self.context.get_bots()
        if not bots:
            logger.warning("群聊总结(增强版): 未找到在线的 Bot 实例，跳过推送。")
            return
        
        # 这里简单取第一个 bot，如果需要特定 bot 推送特定群，需要更复杂的逻辑
        bot = list(bots.values())[0]

        for group_id in self.push_groups:
            # 确保 group_id 是字符串
            g_id_str = str(group_id)
            logger.info(f"群聊总结(增强版): 正在为群 {g_id_str} 生成总结...")
            
            # 调用核心生成逻辑 (silent=True)
            img_result = await self.generate_report(bot, g_id_str, hours=24, silent=True)
            
            if img_result:
                try:
                    # 发送图片
                    payload = {
                        "group_id": int(g_id_str),
                        "message": [
                            {
                                "type": "image",
                                "data": {
                                    "file": img_result
                                }
                            }
                        ]
                    }
                    await bot.api.call_action("send_group_msg", **payload)
                    logger.info(f"群聊总结(增强版): 群 {g_id_str} 推送成功。")
                except Exception as e:
                    logger.error(f"群聊总结(增强版): 群 {g_id_str} 推送失败: {e}")
            
            # 避免触发风控，群与群之间暂停几秒
            await asyncio.sleep(5)

    async def fetch_group_history(self, bot, group_id: str, hours_limit: int = 24):
        """分页获取群聊历史消息"""
        all_messages = []
        message_seq = 0
        cutoff_time = time.time() - (hours_limit * 3600)

        logger.info(f"群聊总结:开始获取群 {group_id} 消息，目标上限: {self.max_msg_count}条 / {self.max_query_rounds}轮")

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

                if oldest_msg_time < cutoff_time:
                    break
            except Exception as e:
                logger.error(f"群聊总结:Fetch loop error: {e}")
                break

        return all_messages

    def process_messages(self, messages: list, hours_limit: int = 24):
        """处理消息并进行黑名单过滤"""
        cutoff_time = time.time() - (hours_limit * 3600)
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                continue

            # 过滤系统消息
            raw_msg = msg.get("raw_message", "")
            if "[CQ:" in raw_msg and "image" in raw_msg: # 简单过滤图片
                pass
            
            sender = msg.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or "未知用户"
            
            # --- 黑名单过滤 (新增功能) ---
            if nickname in self.exclude_users:
                continue
            # 也可以根据 sender['user_id'] 过滤，如需支持QQ号过滤可扩展
            
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
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%Y.%m.%d %H:%M')}] {m['name']}: {m['content']}"
            for m in valid_msgs
        ])
        
        return valid_msgs, top_users, dict(trend_counter), chat_log

    async def generate_report(self, bot, group_id: str, hours: int = 24, silent: bool = False):
        """
        核心生成逻辑：获取消息 -> 分析 -> 渲染图片
        返回: 图片的 URL/Path/Base64 (取决于 render 结果) 或 None
        """
        try:
            group_info = await bot.api.call_action("get_group_info", group_id=group_id)
        except:
            group_info = {"group_name": "未知群聊"}

        # 1. 获取消息
        raw_messages = await self.fetch_group_history(bot, group_id, hours_limit=hours)
        if not raw_messages:
            if not silent: logger.warning(f"群 {group_id} 无法获取历史消息")
            return None

        # 2. 处理数据 (含黑名单过滤)
        valid_msgs, top_users, trend, chat_log = self.process_messages(raw_messages, hours_limit=hours)
        if not valid_msgs:
            if not silent: logger.warning(f"群 {group_id} 无有效聊天记录")
            return None

        if len(chat_log) > self.msg_token_limit:
            chat_log = chat_log[:self.msg_token_limit]

        # 3. LLM 请求
        prompt = f"""
        你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（最近{hours}小时），生成一份总结数据。
        
        【要求】：
        1. 分析 3-8 个主要话题，每个话题包含：时间段（如2026-01-15 10:00 ~ 2026-01-15 11:00）和简短内容。
        2. 写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性。
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

        # 4. 渲染
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
        """手动指令：/总结群聊"""
        hours = 24
        group_id = event.get_group_id()
        
        # 1. 发送提示 (仅手动模式)
        yield event.plain_result(f"🌱 正在连接神经云端，回溯最近 {hours} 小时的记忆...")
        
        # 2. 调用核心逻辑
        img_result = await self.generate_report(event.bot, group_id, hours, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 总结生成失败，可能是没有聊天记录或配置错误。")

    # --- Tool 入口 ---
    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, hours: int = 24):
        """LLM调用工具"""
        group_id = event.get_group_id()
        yield event.plain_result(f"🌱 正在分析最近 {hours} 小时的群聊内容...")
        
        img_result = await self.generate_report(event.bot, group_id, hours, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("没有找到足够的聊天记录来生成总结。")
