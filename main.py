import os
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
from astrbot.api.all import *
from astrbot.api import logger

# 数据存储路径
DATA_DIR = Path("data/plugin_data/astrbot_plugin_safety")
DATA_FILE = DATA_DIR / "users.json"


@register("astrbot_plugin_safety", "shskjw", "噢耶，今天又活一天", "1.0.0")
class SafetyPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.check_interval = config.get("check_interval", 3600)
        self.admins = []
        global_config = context.get_config()
        if global_config and "admins_id" in global_config:
            for admin_id in global_config["admins_id"]:
                # 确保转为字符串且是数字
                if str(admin_id).isdigit():
                    self.admins.append(str(admin_id))

        logger.info(f"[Safety] 加载管理员列表: {self.admins}")

        # 初始化数据文件
        if not DATA_DIR.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not DATA_FILE.exists():
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)

        # 启动后台监控任务
        self.monitor_task = asyncio.create_task(self._monitor_loop())

    # ================= 工具方法 =================

    def _load_users(self) -> dict:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取用户数据失败: {e}")
            return {}

    def _save_users(self, data: dict):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _update_activity(self, user_id: str, group_id: str = None, bot_id: str = None):
        """更新用户活跃时间"""
        users = self._load_users()
        if user_id in users:
            users[user_id]["last_active"] = time.time()
            users[user_id]["alert_level"] = 0  # 重置报警等级
            if group_id: users[user_id]["group_id"] = group_id
            if bot_id: users[user_id]["bot_id"] = bot_id
            self._save_users(users)
            return True
        return False

    def _format_time(self, timestamp):
        """将时间戳转换为可读格式"""
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

    def _format_duration(self, seconds):
        """将秒数转换为天/小时"""
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}天 {hours}小时"

    # ================= 管理员指令 =================

    @command("安全监控列表")
    async def cmd_admin_check(self, event: AstrMessageEvent):
        """(管理员) 查看所有用户的监控状态"""
        sender_id = event.get_sender_id()

        # 权限校验
        if sender_id not in self.admins:
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return

        users = self._load_users()
        if not users:
            yield event.plain_result("📂 当前没有正在监控的用户。")
            return

        # 构建报表
        msg_lines = ["📋 [管理员] 全员安全监控报表", "----------------"]
        now = time.time()

        for uid, info in users.items():
            last_active = info.get("last_active", 0)
            diff = now - last_active
            level = info.get("alert_level", 0)
            max_days = info.get("max_missing_days", 3)
            contact = info.get("emergency_contact", "未设置")

            # 状态描述
            status_emoji = "🟢"
            status_text = "正常"
            if level == 1:
                status_emoji = "🟡"
                status_text = "警告中"
            elif level == 2:
                status_emoji = "🔴"
                status_text = "已失联(已报警)"

            line = (
                f"{status_emoji} 用户: {uid}\n"
                f"   ├ 状态: {status_text} (Lv.{level})\n"
                f"   ├ 失联时长: {self._format_duration(diff)}\n"
                f"   ├ 最后活跃: {self._format_time(last_active)}\n"
                f"   ├ 设定阈值: {max_days}天\n"
                f"   └ 紧急联系人: {contact}"
            )
            msg_lines.append(line)

        yield event.plain_result("\n".join(msg_lines))

    # ================= 用户指令交互 =================

    @command("注册又活一天")
    async def cmd_register(self, event: AstrMessageEvent):
        """用户注册或手动打卡"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id() if event.message_obj.group_id else ""
        bot_id = event.bot.id

        users = self._load_users()

        if user_id not in users:
            users[user_id] = {
                "user_id": user_id,
                "bot_id": bot_id,
                "group_id": group_id,
                "emergency_contact": "",
                "max_missing_days": 3,
                "last_active": time.time(),
                "alert_level": 0
            }
            msg = "✅ 注册成功！监控已启动。\n请尽快发送 /配置紧急联系人 [QQ号] 完善安全设置。"
        else:
            users[user_id]["last_active"] = time.time()
            users[user_id]["alert_level"] = 0
            users[user_id]["bot_id"] = bot_id
            if group_id: users[user_id]["group_id"] = group_id
            msg = "✅ 打卡成功！计时器已重置。"

        self._save_users(users)
        yield event.plain_result(msg)

    @command("配置紧急联系人")
    async def cmd_set_contact(self, event: AstrMessageEvent, contact_qq: str):
        """配置紧急联系人QQ"""
        user_id = event.get_sender_id()
        users = self._load_users()

        if user_id not in users:
            yield event.plain_result("❌ 请先发送 /注册又活一天 开启功能。")
            return

        if not contact_qq.isdigit():
            yield event.plain_result("❌ 联系人必须是QQ号（纯数字）。")
            return

        users[user_id]["emergency_contact"] = contact_qq
        self._save_users(users)
        yield event.plain_result(f"✅ 紧急联系人已设置为: {contact_qq}")

    @command("设置失联时间")
    async def cmd_set_days(self, event: AstrMessageEvent, days: int):
        """自定义最大失联天数"""
        user_id = event.get_sender_id()
        users = self._load_users()

        if user_id not in users:
            yield event.plain_result("❌ 请先发送 /注册又活一天 开启功能。")
            return

        users[user_id]["max_missing_days"] = int(days)
        self._save_users(users)
        yield event.plain_result(f"✅ 设置成功。若 {days} 天无消息，将联系紧急联系人。")

    # ================= 被动监听 =================

    @event_handler()
    async def on_user_message(self, event: AstrMessageEvent):
        """监听所有消息，如果是注册用户，悄悄更新时间"""
        user_id = event.get_sender_id()
        self._update_activity(user_id)

    # ================= 核心后台逻辑 =================

    async def _send_private_raw(self, bot, user_id, text):
        """私聊发送逻辑"""
        try:
            # logger.info(f"[Safety] 尝试私聊用户 {user_id}")
            await bot.send_private_msg(
                user_id=int(user_id),
                message=[{"type": "text", "data": {"text": text}}]
            )
        except Exception as e:
            logger.error(f"[Safety] 私聊发送失败: {e}")

    async def _send_group_at_raw(self, bot, group_id, user_id, text):
        """群聊@逻辑"""
        try:
            # logger.info(f"[Safety] 尝试群聊@用户 {user_id}")
            await bot.send_group_msg(
                group_id=int(group_id),
                message=[
                    {"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": f" {text}"}}
                ]
            )
        except Exception as e:
            logger.error(f"[Safety] 群聊发送失败: {e}")

    async def _check_user_in_group(self, bot, group_id, user_id):
        """检查用户是否在群里"""
        if not group_id: return False
        try:
            member = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
            return member is not None
        except:
            return False

    async def _monitor_loop(self):
        """后台定时任务"""
        while True:
            await asyncio.sleep(self.check_interval)

            users = self._load_users()
            now = time.time()
            dirty = False

            for uid, info in users.items():
                last = info.get("last_active", now)
                diff = now - last
                level = info.get("alert_level", 0)
                max_days = info.get("max_missing_days", 3)
                bot_id = info.get("bot_id")

                bot = self.context.get_bot(bot_id)
                if not bot: continue

                # 阶段 1: 警告 (超过 24小时 且 Level < 1)
                if diff > 86400 and level < 1:
                    if info.get("group_id"):
                        await self._send_group_at_raw(bot, info["group_id"], uid,
                                                      "⚠️ 你已经24小时没说话了，还活着吗？请冒个泡！")
                    await self._send_private_raw(bot, uid,
                                                 "⚠️ [安全提醒] 你已经一天没说话了，请回复任意消息报平安。")
                    info["alert_level"] = 1
                    dirty = True

                # 阶段 2: 紧急 (超过 max_missing_days 且 Level < 2)
                elif diff > (max_days * 86400) and level < 2:
                    contact_id = info.get("emergency_contact")

                    if contact_id:
                        msg_text = f"🚨 [紧急求助] 用户 {uid} 已失联超过 {max_days} 天！"
                        is_in_group = await self._check_user_in_group(bot, info["group_id"], contact_id)

                        if is_in_group:
                            await self._send_group_at_raw(bot, info["group_id"], contact_id,
                                                          f"警告：用户 {uid} 已失联，请尝试联系！")
                            await self._send_private_raw(bot, contact_id, msg_text + " (已在群内同步提醒)")
                        else:
                            await self._send_private_raw(bot, contact_id, msg_text + " (请尝试通过电话联系他)")

                    info["alert_level"] = 2
                    dirty = True

            if dirty:
                self._save_users(users)