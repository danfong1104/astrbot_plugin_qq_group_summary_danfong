import json
import os
import re
import datetime
import traceback
import asyncio
import base64
import textwrap
import urllib.parse
from pathlib import Path
from collections import Counter
from typing import List, Optional, Any, Dict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- 常量配置 ---
VERSION = "0.1.30"
MAX_RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.5
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
ESTIMATED_CHARS_PER_TOKEN = 2  # 中文环境估算
LLM_TIMEOUT = 60

def _parse_llm_json(text: str) -> dict:
    """鲁棒性 JSON 解析器：寻找最外层的 {} 对"""
    text = text.strip()
    # 1. 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 移除 Markdown 标记后尝试
    text_clean = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        pass

    # 3. 栈式寻找 (最稳健)
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

    raise ValueError(f"无法提取有效 JSON，响应片段: {text[:20]}...")

@register("group_summary_danfong", "Danfong", "群聊总结增强版", "0.1.30")
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

    def _load_template(self) -> str:
        try:
            if not self.template_path.exists():
                raise FileNotFoundError(f"模板文件缺失: {self.template_path}")
            return self.template_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 模板加载失败: {e}")
            return "<h1>Template Error</h1>"

    def setup_schedule(self):
        """配置定时任务 (支持热重载)"""
        try:
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
                logger.error(f"群聊总结({VERSION}): 时间格式错误 (应为 HH:MM)")
        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 调度器启动失败: {e}")

    def terminate(self):
        """插件卸载/重载清理钩子"""
        try:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)
                logger.info(f"群聊总结({VERSION}): 定时任务已停止")
        except Exception:
            pass

    async def _get_bot(self, event: Optional[AstrMessageEvent] = None) -> Optional[Any]:
        """统一获取 Bot (加锁保护)"""
        async with self._bot_lock:
            # 1. 优先当前事件
            if event and event.bot:
                self._global_bot = event.bot
                return event.bot
            # 2. 缓存
            if self._global_bot:
                return self._global_bot
            # 3. 兜底：从 Context 找一个 OneBot 适配器
            try:
                if hasattr(self.context, "get_bots"):
                    bots = self.context.get_bots()
                    for bot in bots.values():
                        p_name = getattr(bot, "platform_name", "").lower()
                        if "qq" in p_name or "onebot" in p_name or "napcat" in p_name:
                            self._global_bot = bot
                            return bot
                    # 实在没有，随便拿一个
                    if bots:
                        self._global_bot = next(iter(bots.values()))
                        return self._global_bot
            except Exception:
                pass
            return None

    # ================= 兼容性修复 =================
    
    async def html_render(self, template: str, data: dict, options: dict = None):
        """兼容 Star 基类缺失 html_render"""
        if hasattr(self.context, "image_renderer"):
            return await self.context.image_renderer.render(template, data, **(options or {}))
        return None

    # ================= 交互入口 =================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def capture_bot_instance(self, event: AstrMessageEvent, *args, **kwargs):
        """被动捕获 (带 *args 兼容)"""
        if not self._global_bot:
            await self._get_bot(event)

    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def summarize_group(self, event: AstrMessageEvent, *args, **kwargs):
        """
        手动指令
        修复: 移除 @event_message_type 避免重复调用
        修复: 添加 *args, **kwargs 吞掉 AstrBot 传入的多余参数
        """
        bot = await self._get_bot(event)
        if not bot:
            yield event.plain_result("❌ 系统未就绪 (Bot实例丢失)")
            return
            
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用。")
            return

        yield event.plain_result(f"🌱 正在回溯记忆并生成报告...")
        
        # 针对该群加锁，防止重复指令
        lock = self._group_locks.setdefault(group_id, asyncio.Lock())
        if lock.locked():
            yield event.plain_result("⚠️ 该群正在生成中，请勿重复触发。")
            return

        async with lock:
            img_result = await self.generate_report(bot, group_id, silent=False)
        
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("❌ 生成失败，请检查日志 (可能是历史消息获取失败)。")

    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, *args, **kwargs):
        """LLM 工具调用"""
        bot = await self._get_bot(event)
        group_id = event.get_group_id()
        if not group_id or not bot:
            yield event.plain_result("无法生成总结。")
            return

        yield event.plain_result(f"🌱 正在分析群聊内容...")
        lock = self._group_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            img_result = await self.generate_report(bot, group_id, silent=False)
            
        if img_result:
            yield event.image_result(img_result)
        else:
            yield event.plain_result("总结生成失败。")

    # ================= 核心逻辑 =================

    async def _fetch_messages(self, bot, group_id: str, start_ts: float) -> List[dict]:
        """获取消息 (带协议检查)"""
        # 检查是否为 OneBot 协议
        p_name = getattr(bot, "platform_name", "").lower()
        if not any(x in p_name for x in ["qq", "onebot", "napcat", "llonebot", "aiocqhttp"]):
            logger.warning(f"群聊总结({VERSION}): 适配器 {p_name} 可能不支持 get_group_msg_history")

        all_msgs = []
        msg_seq = 0
        seen_ids = set()

        for _ in range(self.max_query_rounds):
            if len(all_msgs) >= self.max_msg_count: break

            try:
                resp = await bot.api.call_action("get_group_msg_history", 
                    group_id=group_id, count=200, message_seq=msg_seq, reverseOrder=True)
                
                if not resp or "messages" not in resp: break
                batch = resp["messages"]
                if not batch: break

                # 统一排序: 时间倒序 (最新的在前)
                batch.sort(key=lambda x: x.get('time', 0), reverse=True)
                
                oldest = batch[-1]
                msg_seq = oldest.get('message_seq') # 更新游标
                
                # 去重添加
                for m in batch:
                    mid = m.get('message_id')
                    if mid and mid not in seen_ids:
                        all_msgs.append(m)
                        seen_ids.add(mid)
                
                # 时间截止检查
                if oldest.get('time', 0) < start_ts:
                    break
                    
            except Exception as e:
                # 忽略不支持 API 的错误，避免刷屏
                if "404" not in str(e) and "ActionFailed" not in str(e):
                    logger.error(f"群聊总结({VERSION}): 获取消息失败: {e}")
                break
                
        return all_msgs

    def _process_data(self, messages: List[dict], start_ts: float) -> Tuple[Any, Any, Any, str]:
        """数据处理"""
        valid_msgs = []
        u_count = Counter()
        t_count = Counter()
        
        for m in messages:
            if m.get('time', 0) < start_ts: continue
            
            raw = m.get('raw_message', "")
            # 清洗 CQ 码保留文本
            content = re.sub(r'\[CQ:[^\]]+\]', '', raw).strip()
            if not content: continue
            
            sender = m.get('sender', {})
            nick = sender.get('card') or sender.get('nickname') or "未知"
            uid = sender.get('user_id')
            
            # 黑名单
            if nick in self.exclude_users or (uid and str(uid) in self.exclude_users):
                continue

            valid_msgs.append({"time": m['time'], "name": nick, "content": content})
            u_count[nick] += 1
            t_count[datetime.datetime.fromtimestamp(m['time']).strftime("%H")] += 1

        top_users = [{"name": k, "count": v} for k, v in u_count.most_common(5)]
        # LLM 需要时间正序
        valid_msgs.sort(key=lambda x: x['time'])
        
        # 智能截断
        max_items = int(self.msg_token_limit / ESTIMATED_CHARS_PER_TOKEN)
        msgs_for_llm = valid_msgs[-max_items:] if len(valid_msgs) > max_items else valid_msgs
        
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%H:%M')}] {m['name']}: {m['content']}"
            for m in msgs_for_llm
        ])
        
        return valid_msgs, top_users, dict(t_count), chat_log

    async def _run_llm(self, chat_log: str) -> Optional[dict]:
        """LLM 分析 (带重试)"""
        provider = self.context.get_provider_by_id(self.config.get("provider_id")) or self.context.get_using_provider()
        if not provider: return None

        style = self.config.get("summary_prompt_style", "")
        if "{bot_name}" in style: style = style.replace("{bot_name}", self.bot_name)
        if not style: style = f"写一段{self.bot_name}的风格点评。"

        prompt = textwrap.dedent(f"""
            角色：{self.bot_name}。任务：群聊总结。
            要求：
            1. 提取3-8个话题(时间段+摘要)。
            2. {style}
            3. 严格返回JSON：{{"topics": [{{"time_range":"", "summary":""}}], "closing_remark":""}}
            
            记录：
            {chat_log}
        """).strip()

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
                logger.error(f"群聊总结({VERSION}): LLM重试 {i+1}: {e}")
        return None

    async def generate_report(self, bot, group_id: str, silent: bool = False) -> Optional[str]:
        try:
            today_ts = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            
            # 获取信息
            try:
                g_info = await bot.api.call_action("get_group_info", group_id=group_id)
            except:
                g_info = {"group_name": "未知群聊"}

            # 拉取
            raw_msgs = await self._fetch_messages(bot, group_id, today_ts)
            if not raw_msgs:
                if not silent: logger.warning(f"群聊总结({VERSION}): 无消息")
                return None

            # 处理
            _, top_users, trend, chat_log = self._process_data(raw_msgs, today_ts)
            if not chat_log:
                if not silent: logger.warning(f"群聊总结({VERSION}): 无有效文本")
                return None

            # 分析
            analysis = await self._run_llm(chat_log)
            if not analysis:
                analysis = {"topics": [], "closing_remark": "分析超时或失败。"}

            # 渲染
            render_data = {
                "date": datetime.datetime.now().strftime("%Y.%m.%d"),
                "top_users": top_users,
                "trend": trend,
                "topics": analysis.get("topics", []),
                "summary_text": analysis.get("closing_remark", ""),
                "group_name": g_info.get("group_name", "群聊"),
                "bot_name": self.bot_name
            }
            
            options = {"quality": 95, "device_scale_factor_level": "ultra", "viewport_width": 500}
            return await self.html_render(self.html_template, render_data, options=options)

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 流程异常: {traceback.format_exc()}")
            return None

    async def run_scheduled_task(self):
        """定时任务"""
        try:
            logger.info(f"群聊总结({VERSION}): 定时任务触发")
            bot = await self._get_bot()
            if not bot:
                logger.warning(f"群聊总结({VERSION}): 无可用 Bot")
                return

            if not self.push_groups: return

            for gid in self.push_groups:
                g_str = str(gid)
                lock = self._group_locks.setdefault(g_str, asyncio.Lock())
                if lock.locked(): continue # 跳过正在生成的群

                async with lock:
                    try:
                        img = await self.generate_report(bot, g_str, silent=True)
                        if img:
                            cq = ""
                            if img.startswith("http"):
                                cq = f"[CQ:image,file={img}]"
                            else:
                                # 路径与内存检查
                                p_obj = Path(urllib.parse.urlparse(img).path)
                                if os.name == 'nt' and str(p_obj).startswith('\\'):
                                    p_obj = Path(str(p_obj).lstrip('\\'))
                                
                                if p_obj.exists():
                                    if p_obj.stat().st_size > MAX_IMAGE_SIZE_BYTES:
                                        logger.error(f"图片过大跳过")
                                        continue
                                    with open(p_obj, "rb") as f:
                                        b64 = base64.b64encode(f.read()).decode('utf-8')
                                    cq = f"[CQ:image,file=base64://{b64}]"
                            
                            if cq:
                                await bot.api.call_action("send_group_msg", group_id=int(g_str), message=cq)
                                logger.info(f"群聊总结({VERSION}): 群 {g_str} 推送成功")
                    except Exception as e:
                        logger.error(f"群聊总结({VERSION}): 推送失败 {e}")
                
                await asyncio.sleep(PUSH_DELAY_BETWEEN_GROUPS)

        except Exception as e:
            logger.error(f"群聊总结({VERSION}): 任务崩溃: {traceback.format_exc()}")
