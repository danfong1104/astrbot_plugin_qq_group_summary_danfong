import json
import os
import re
import time
import datetime
import traceback
from collections import Counter
from tarfile import data_filter

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


# 解析JSON
def _parse_llm_json(text: str) -> dict:
    """
    尝试从 LLM 的回复中提取并解析 JSON。
    支持处理 markdown 代码块、前后无关文本等情况。
    """
    try:
        # 1. 尝试直接解析（万一 LLM 很听话）
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        # 2. 使用正则提取第一个 { 到最后一个 } 之间的内容
        # [\s\S] 匹配任意字符包括换行符，* 贪婪匹配确保拿到完整的 JSON 对象
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            json_str = match.group()
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # 3. 如果还是失败，抛出异常或返回空
    raise ValueError("无法从 LLM 回复中提取有效的 JSON 数据")


@register("group_summary", "棒棒糖", "群聊总结", "1.1.3")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.max_msg_count = self.config.get("max_msg_count", 2000)
        self.max_query_rounds = self.config.get("max_query_rounds", 10)
        self.bot_name = self.config.get("bot_name", "纱织")
        self.msg_token_limit = self.config.get("token_limit", 6000)
        # 获取当前文件 (main.py) 所在的目录
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # 拼接模板文件路径: group_summary/templates/report.html
        template_path = os.path.join(current_dir, "templates", "report.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                self.html_template = f.read()
            logger.info(f"群聊总结:成功加载群聊总结模板: {template_path}")
        except FileNotFoundError:
            logger.error(f"群聊总结:未找到模板文件: {template_path}")
            # 设置一个简单的兜底模板，防止崩溃
            self.html_template = "<h1>Template Not Found</h1>"

    async def fetch_group_history(self, bot, group_id: str, hours_limit: int = 24):
        """分页获取群聊历史消息"""
        all_messages = []
        message_seq = 0
        cutoff_time = time.time() - (hours_limit * 3600)

        logger.info(f"群聊总结:开始获取群 {group_id} 消息，目标上限: {self.max_msg_count}条 / {self.max_query_rounds}轮")

        for round_idx in range(self.max_query_rounds):
            # 1. 检查总数是否超标
            if len(all_messages) >= self.max_msg_count:
                break

            try:
                # 2. 构造 API 参数
                params = {
                    "group_id": group_id,
                    "count": 200,
                    "message_seq":message_seq,
                    "reverseOrder": True,
                }
                logger.info(f"群聊总结:Round {round_idx+1}: 获取参数: {params}")
                # 3. 调用 API
                resp: dict = await bot.api.call_action("get_group_msg_history", **params)

                round_messages = resp["messages"]
                if not round_messages:
                    break
                batch_msgs = round_messages
                # 更新 seq 以获取更早的消息
                # 假设返回的消息是按时间倒序或正序，我们需要找到最“旧”的一条的ID
                # NapCat get_group_msg_history 通常返回的是 [oldest ... newest]
                # 翻页时，通常取最旧一条的 seq 作为下一次的起点
                oldest_msg_time = batch_msgs[-1].get("time", 0)
                newest_msg_time = batch_msgs[0].get("time", 0)
                logger.info(f"群聊总结:Round {round_idx+1}: 最旧消息时间: {oldest_msg_time}")
                logger.info(f"群聊总结:Round {round_idx+1}: 最新消息时间: {newest_msg_time}")
                # 接口不兼容的预防代码
                message_seq = round_messages[-1]["message_seq"]
                if oldest_msg_time > newest_msg_time:
                    message_seq = batch_msgs[0]["message_seq"]
                    oldest_msg_time = newest_msg_time
                logger.info(f"群聊总结:本次获取到的最旧一条message_seq:{message_seq}")
                logger.info(f"群聊总结:Round {round_idx+1}: 获取到 {len(batch_msgs)} 条消息")
                if not batch_msgs:
                    break # 没有更多消息了

                # NapCat 请求的倒数数据，是新->旧的顺序
                # 我们需要把这批消息加到总列表里
                # 注意：如果是翻页获取，新获取的批次应该放在总列表的最前面，或者最后统一按时间排序
                all_messages.extend(batch_msgs)

                # 如果这一轮抓取的最旧消息都还在 cutoff 之前，说明已经抓够了时间范围
                if oldest_msg_time < cutoff_time:
                    # 虽然这一批里可能有一部分有效，但下一轮肯定都是无效的了，标记结束
                    break

                # 简单的进度日志
                logger.info(f"群聊总结:Round {round_idx+1}: 获取到 {len(batch_msgs)} 条消息")

            except Exception as e:
                logger.error(f"群聊总结:Error: {traceback.format_exc()}")
                logger.info(f"群聊总结:Fetch loop error: {e}")
                break

        return all_messages

    def process_messages(self, messages: list, hours_limit: int = 24):
        """纯 Python 统计数据"""
        cutoff_time = time.time() - (hours_limit * 3600)
        logger.info(f"群聊总结:开始处理 {len(messages)} 条消息，聊天截止时间戳为: {cutoff_time} ")
        valid_msgs = []
        user_counter = Counter()
        trend_counter = Counter()
        filter_date_count = 0
        filter_sys_msg_count = 0
        for msg in messages:
            ts = msg.get("time", 0)
            if ts < cutoff_time:
                filter_date_count += 1
                continue

            # 过滤QQ转发和图片信息
            if "[CQ:" in msg.get("raw_message"):
                filter_sys_msg_count += 1
                continue

            sender = msg.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or "未知用户"
            content = msg.get("raw_message") or ""

            # 1. 收集有效消息
            valid_msgs.append({
                "time": ts,
                "name": nickname,
                "content": content
            })

            # 2. 统计用户发言数
            user_counter[nickname] += 1

            # 3. 统计小时趋势
            hour_str = datetime.datetime.fromtimestamp(ts).strftime("%H")
            trend_counter[str(int(hour_str))] += 1

        # 整理 Top 5
        top_users = [{"name": name, "count": count} for name, count in user_counter.most_common(5)]

        # 整理 LLM 日志文本
        chat_log = "\n".join([
            f"[{datetime.datetime.fromtimestamp(m['time']).strftime('%Y.%m.%d %H:%M')}] {m['name']}: {m['content']}"
            for m in valid_msgs
        ])
        logger.info(f"群聊总结:共获取到{len(valid_msgs)}条有效消息,过滤{filter_date_count}条时间超出限制消息,过滤{filter_sys_msg_count}条系统消息")
        return valid_msgs, top_users, dict(trend_counter), chat_log



    # --- 核心逻辑生成器 (供 Command 和 Tool 复用) ---
    async def _summary_logic(self, event: AstrMessageEvent, hours: int = 24):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 只有在群聊中才能使用总结功能哦。")
            return

        yield event.plain_result(f"🌱 正在连接神经云端，回溯最近 {hours} 小时的记忆...")

        try:
            group_info = await event.bot.api.call_action("get_group_info", group_id=group_id)
        except:
            group_info = {"group_name": "未知群聊"}

        # 1. 获取消息
        raw_messages = await self.fetch_group_history(event.bot, group_id, hours_limit=hours)
        if not raw_messages:
            yield event.plain_result("⚠️ 无法获取历史消息，可能是API受限或记录为空。")
            return

        # 2. 处理数据
        valid_msgs, top_users, trend, chat_log = self.process_messages(raw_messages, hours_limit=hours)
        if not valid_msgs:
            yield event.plain_result(f"在最近 {hours} 小时内没有发现聊天记录。")
            return

        if len(chat_log) > self.msg_token_limit:
            logger.warning(f"群聊总结:LLM 日志长度超过限制:{len(chat_log)}，已截断。")
            chat_log = chat_log[:self.msg_token_limit]

        # 3. LLM Prompt
        prompt = f"""
        你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（最近{hours}小时），生成一份总结数据。

        【要求】：
        1. 分析 3-8 个主要话题，每个话题包含：时间段（如2026-01-15 10:00 ~ 2026-01-15 11:00）和简短内容。
        2. 写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性。
        3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}

        【聊天记录】：
        {chat_log}
        """

        yield event.plain_result(f"☁️ 已获取 {len(valid_msgs)} 条消息，正在生成分析报告...")
        logger.info(f"群聊总结:本次获取的聊天记录：{chat_log}")
        # 4. 调用 LLM
        try:
            provider = self.context.get_provider_by_id(
                self.config.get("provider_id")) or self.context.get_using_provider()
            if not provider:
                yield event.plain_result("❌ 未配置用于文本生成任务的 LLM 提供商。")
                return

            response = await provider.text_chat(prompt, session_id=None)
            logger.info(f"群聊总结:LLM 原始回复: {response.completion_text}")  # 建议保留日志以便调试
            analysis_data = _parse_llm_json(response.completion_text)
        except Exception as e:
            logger.error(f"群聊总结:Traceback Error: {traceback.format_exc()}")
            logger.error(f"群聊总结:LLM Error: {e}")
            analysis_data = {"topics": [], "closing_remark": "纱织姐姐有点累了，没能写出总结..."}

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
            img_result = await self.html_render(self.html_template, render_data, options=options)
            yield event.image_result(img_result)
        except Exception as e:
            logger.error(f"群聊总结:Render Error: {traceback.format_exc()}")
            yield event.plain_result(f"❌ 渲染失败: {e}")

    # --- 1. 指令入口 ---
    @filter.command("总结群聊")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def summarize_group(self, event: AstrMessageEvent):
        """
        手动指令：/总结群聊
        """
        async for result in self._summary_logic(event, hours=24):
            yield result

    # --- 2. Tool (Function Call) 入口 ---
    @filter.llm_tool(name="group_summary_tool")
    async def call_summary_tool(self, event: AstrMessageEvent, hours: int = 24):
        """
        总结当前群聊。当用户询问“今天群里发生了什么”、“总结一下群聊”、“大家在聊什么”时调用此工具。

        Args:
            hours (int): 总结过去多少小时的消息。默认为 24。
        """
        # Tool 的执行结果需要通过 yield 返回给用户
        # 最后的 return 字符串会作为 Tool Output 给 LLM
        async for result in self._summary_logic(event, hours=hours):
            yield result