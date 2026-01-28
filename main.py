import json
import os
import re
import datetime
import time
import traceback
import asyncio
import jinja2
import base64
from collections import Counter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

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

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.49")
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
            
        # 依赖检测
        try:
            import playwright
            logger.info("群聊总结(增强版): 依赖环境检测正常。")
        except:
            logger.error("群聊总结(增强版): ⚠️ 未检测到 Playwright，请确保已执行安装命令。")

        # 定时任务
        self.scheduler = AsyncIOScheduler()
        if self.enable_auto_push:
            self.setup_schedule()

    # --- 核心修改1：精准截图，解决留白问题 ---
    async def render_locally(self, html_template: str, data: dict):
        from playwright.async_api import async_playwright
        
        try:
            template = jinja2.Template(html_template)
            html_content = template.render(**data)
        except Exception as e:
            logger.error(f"模板渲染失败: {e}")
            return None

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
                page = await browser.new_page(
                    viewport={"width": 500, "height": 2000}, # 初始画布给大点无所谓
                    device_scale_factor=2
                )
                
                await page.set_content(html_content)
                await page.wait_for_load_state("networkidle")
                
                # --- 修复点：只截取 .container 元素 ---
                # 这样图片高度就会自动适应内容，不会多出一像素空白
                locator = page.locator(".container")
                
                # 临时文件路径
                plugin_dir = os.path.dirname(os.path.abspath(__file__))
                temp_filename = f"summary_temp_{int(time.time())}.jpg"
                save_path = os.path.join(plugin_dir, temp_filename)
                
                # 对定位到的元素截图
                await locator.screenshot(path=save_path, type="jpeg", quality=90)
                await browser.close()
                
                return save_path
                
            except Exception as e:
                logger.error(f"本地渲染失败: {e}")
                return None

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

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        group_id = event.get_group_id()
        
        if not group_id: 
            yield event.plain_result("请在群聊使用")
            return
        
        yield event.plain_result("🌱 正在连接神经云端，回溯今日记忆...")
        img_path = await self.generate_report(event.bot, group_id)
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
            # 这里的清理稍微延后一点点，防止文件还在占用
            await asyncio.sleep(1)
            try: os.remove(img_path)
            except: pass
        else:
            yield event.plain_result("❌ 生成失败，请检查后台日志。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent):
        if not self.global_bot: self.global_bot = event.bot
        group_id = event.get_group_id()
        
        if not group_id: 
            yield event.plain_result("仅限群聊")
            return
        
        yield event.plain_result("🌱 正在分析...")
        img_path = await self.generate_report(event.bot, group_id)
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
            await asyncio.sleep(1)
            try: os.remove(img_path)
            except: pass
        else:
            yield event.plain_result("生成失败")

    # --- 核心修改2：修复定时任务发送逻辑 ---
    async def run_scheduled_task(self):
        if not self.global_bot or not self.push_groups: return
        
        logger.info(f"群聊总结(增强版): 开始执行定时推送，目标群: {self.push_groups}")
        
        for gid in self.push_groups:
            img_path = await self.generate_report(self.global_bot, str(gid), silent=True)
            if img_path and os.path.exists(img_path):
                try:
                    # 读取图片转 Base64，彻底解决 Docker 路径不通的问题
                    with open(img_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    
                    # 使用 base64 协议发送
                    await self.global_bot.api.call_action(
                        "send_group_msg", 
                        group_id=int(gid), 
                        message=f"[CQ:image,file=base64://{b64}]"
                    )
                    logger.info(f"群聊总结(增强版): 群 {gid} 推送成功")
                except Exception as e:
                    logger.error(f"群聊总结(增强版): 群 {gid} 发送失败: {e}")
                
                # 清理临时文件
                try: os.remove(img_path)
                except: pass
            else:
                logger.warning(f"群聊总结(增强版): 群 {gid} 生成图片失败或无内容")
                
            await asyncio.sleep(5)

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
                
                oldest = batch[-1].get("time", 0)
                newest = batch[0].get("time", 0)
                seq = batch[-1]["message_seq"]
                if oldest > newest:
                    seq = batch[0]["message_seq"]
                    oldest = newest
                
                msgs.extend(batch)
                if oldest < start: break
            except: break
        
        valid = []
        users = Counter()
        trend = Counter()
        for m in msgs:
            if m.get("time", 0) < start: continue
            raw = m.get("raw_message", "")
            nick = m.get("sender", {}).get("card") or m.get("sender", {}).get("nickname") or "用户"
            if nick in self.exclude_users: continue
            
            content = raw[:200].replace("\n", " ") 
            valid.append({"time": m["time"], "name": nick, "content": content})
            users[nick] += 1
            trend[str(int(datetime.datetime.fromtimestamp(m["time"]).strftime("%H")))] += 1
            
        valid.sort(key=lambda x: x["time"])
        chat_log = "\n".join([f"[{datetime.datetime.fromtimestamp(v['time']).strftime('%H:%M')}] {v['name']}: {v['content']}" for v in valid])
        return valid, [{"name": k, "count": v} for k,v in users.most_common(5)], trend, chat_log

    async def generate_report(self, bot, group_id, silent=False):
        try:
            info = await bot.api.call_action("get_group_info", group_id=group_id)
        except: info = {"group_name": "群聊"}
        
        res = await self.get_data(bot, group_id)
        if not res or not res[0]: return None
        valid_msgs, top_users, trend, chat_log = res
        
        if len(chat_log) > self.msg_token_limit: chat_log = chat_log[-self.msg_token_limit:]

        style = self.summary_prompt_style.replace("{bot_name}", self.bot_name) or f"{self.bot_name}的温暖总结"
        prompt = f"分析以下群聊(日期{datetime.date.today()})。\n要求：3-5个话题(时间+内容)，一段{style}。\n格式JSON：{{\"topics\":[{{\"time_range\":\"\",\"summary\":\"\"}}],\"closing_remark\":\"\"}}\n记录：\n{chat_log}"
        
        data = {}
        prov = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if prov:
            try:
                resp = await prov.text_chat(prompt)
                data = _parse_llm_json(resp.completion_text)
            except Exception as e:
                logger.error(f"LLM Error: {e}")
        
        if not data: data = {"topics": [], "closing_remark": "分析失败，请检查模型连接。"}

        render_data = {
            "date": datetime.datetime.now().strftime("%Y.%m.%d"),
            "top_users": top_users,
            "trend": trend,
            "topics": data.get("topics", []),
            "summary_text": data.get("closing_remark", ""),
            "group_name": info.get("group_name"),
            "bot_name": self.bot_name
        }
        
        return await self.render_locally(self.html_template, render_data)
