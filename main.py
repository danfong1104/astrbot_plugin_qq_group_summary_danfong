# ... (前面的代码保持不变) ...

    async def generate_report(self, bot, group_id: str, silent: bool = False):
        """核心生成逻辑"""
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

        # --- 获取自定义提示词 ---
        default_style = f"写一段“{self.bot_name}的悄悄话”作为总结，风格温暖、感性，对今天群里的氛围进行点评。"
        user_style = self.config.get("summary_prompt_style", default_style)
        # 支持用户在配置中使用 {bot_name} 占位符
        if "{bot_name}" in user_style:
            user_style = user_style.format(bot_name=self.bot_name)

        prompt = f"""
        你是一个群聊记录员“{self.bot_name}”。请根据以下的群聊记录（日期：{datetime.datetime.now().strftime('%Y-%m-%d')}），生成一份总结数据。
        
        【要求】：
        1. 分析 3-8 个主要话题，每个话题包含：时间段（如 10:00 ~ 11:00）和简短内容。
        2. {user_style}
        3. 严格返回 JSON 格式：{{"topics": [{{"time_range": "...", "summary": "..."}}],"closing_remark": "..."}}
        
        【聊天记录】：
        {chat_log}
        """
        
        # ... (后续代码保持不变) ...
