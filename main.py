import json
import os
import re
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

# 极简 JSON 解析器
def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE | re.DOTALL).strip()
    try:
        return json.loads(text)
    except:
        try:
            match = re.search(r"\{[\s\S]*\}", text)
            if match: return json.loads(match.group())
        except: pass
    return {}

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.43")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 配置读取
        self.max_msg_count = self.config.get("max_msg_count", 2000)
        self.max_query_rounds = self.config.get("max_query_rounds", 10)
        self.bot_name = self.config.get("bot_name", "BOT")
        self.msg_token_limit = self.config.get("token_limit", 6000)
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
        except:
            self.html_template = "<h1>Template Not Found</h1>"

        # 定时任务
        self.scheduler = AsyncIOScheduler()
        if self.enable_auto_push:
            self.setup_schedule()

    def setup_schedule(self):
        try:
            if self.scheduler.running: self.scheduler.shutdown()
            self.scheduler = AsyncIOScheduler()
            hour, minute = self.push_time.split(":")
            trigger = CronTrigger(hour=int(hour), minute=int(minute))
            self.scheduler.add_job(self.run_scheduled_task, trigger)
            self.scheduler.start()
        except Exception as e:
            logger.error(f"群聊总结: 定时任务错误 {e}")

    # 事件监听
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        group_id = event.get_group_id()
        if not group_id: return yield event.plain_result("请在群聊使用")
        
        yield event.plain_result("🌱 正在连接神经云端，回溯今日记忆...")
        img = await self.generate_report(event.bot, group_id)
        yield event.image_result(img) if img else event.plain_result("❌ 生成失败，请检查日志")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        group_id = event.get_group_id()
        if not group_id: return yield event.plain_result("仅限群聊")
        
        yield event.plain_result("🌱 正在分析...")
        img = await self.generate_report(event.bot, group_id)
        yield event.image_result(img) if img else event.plain_result("生成失败")

    # 定时任务逻辑 (保留你的增强功能)
    async def run_scheduled_task(self):
        if not self.global_bot or not self.push_groups: return
        for gid in self.push_groups:
            img = await self.generate_report(self.global_bot, str(gid), silent=True)
            if img:
                # 兼容不同系统的文件路径处理
                if not img.startswith("http"):
                    path = img.replace("file://", "")
                    if os.name=='nt' and path.startswith('/') and ':' in path: path = path[1:]
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    await self.global_bot.api.call_action("send_group_msg", group_id=int(gid), message=f"[CQ:image,file=base64://{b64}]")
            await asyncio.sleep(5)

    # 数据获取 (保持原逻辑)
    async def get_data(self, bot, group_id):
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0).timestamp()
        msgs = []
        seq = 0
        
        for _ in range(self.max_query_rounds):
            if len(msgs) >= self.max_msg_count: break
            try:
                ret = await bot.api.call_action("get_group_msg_history", group_id=group_id, count=100, message_seq=seq, reverseOrder=True)
                batch = ret.get("messages", [])
                if not batch: break
                
                # 关键修复：确保时间顺序处理正确
                oldest = batch[-1].get("time", 0)
                newest = batch[0].get("time", 0)
                seq = batch[-1]["message_seq"]
                if oldest > newest: # 兼容某些实现的倒序返回
                    seq = batch[0]["message_seq"]
                    oldest = newest
                
                msgs.extend(batch)
                if oldest < start: break
            except: break
        
        # 数据清洗
        valid = []
        users = Counter()
        trend = Counter()
        for m in msgs:
            if m.get("time", 0) < start: continue
            raw = m.get("raw_message", "")
            nick = m.get("sender", {}).get("card") or m.get("sender", {}).get("nickname") or "用户"
            if nick in self.exclude_users: continue
            
            valid.append({"time": m["time"], "name": nick, "content": raw[:100].replace("\n", " ")})
            users[nick] += 1
            trend[str(int(datetime.datetime.fromtimestamp(m["time"]).strftime("%H")))] += 1
            
        valid.sort(key=lambda x: x["time"])
        chat_log = "\n".join([f"[{datetime.datetime.fromtimestamp(v['time']).strftime('%H:%M')}] {v['name']}: {v['content']}" for v in valid])
        return valid, [{"name": k, "count": v} for k,v in users.most_common(5)], trend, chat_log

    # 核心生成逻辑
    async def generate_report(self, bot, group_id, silent=False):
        try:
            info = await bot.api.call_action("get_group_info", group_id=group_id)
        except: info = {"group_name": "群聊"}
        
        res = await self.get_data(bot, group_id)
        if not res or not res[0]: return None
        valid_msgs, top_users, trend, chat_log = res
        
        # 截断日志
        if len(chat_log) > self.msg_token_limit: chat_log = chat_log[-self.msg_token_limit:]

        # 构建 Prompt
        style = self.summary_prompt_style.replace("{bot_name}", self.bot_name) or f"{self.bot_name}的温暖总结"
        prompt = f"分析以下群聊(日期{datetime.date.today()})。\n要求：3-5个话题(时间+内容)，一段{style}。\n格式JSON：{{\"topics\":[{{\"time_range\":\"\",\"summary\":\"\"}}],\"closing_remark\":\"\"}}\n记录：\n{chat_log}"
        
        # LLM 请求
        data = {}
        prov = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if prov:
            try:
                resp = await prov.text_chat(prompt)
                data = _parse_llm_json(resp.completion_text)
            except Exception as e:
                logger.error(f"LLM Error: {e}")
        
        if not data: data = {"topics": [], "closing_remark": "分析失败，请检查模型连接。"}

        # 渲染 (关键修复点)
        render_data = {
            "date": datetime.datetime.now().strftime("%Y.%m.%d"),
            "top_users": top_users,
            "trend": trend,
            "topics": data.get("topics", []),
            "summary_text": data.get("closing_remark", ""),
            "group_name": info.get("group_name"),
            "bot_name": self.bot_name
        }
        
        # --- 重点：这里修复了导致报错的参数 ---
        # 移除了 "ultra"，改为标准的 viewport 和 scale 参数
        options = {
            "viewport": {"width": 500, "height": 1500},
            "device_scale_factor": 2
        }
        
        return await self.html_render(self.html_template, render_data, options=options)
