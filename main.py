import os
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
from shutil import copyfile

from astrbot.api.all import *
from astrbot.api.event import filter
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

        # --- 加载管理员列表 ---
        self.admins = []
        global_config = context.get_config()
        if global_config and "admins_id" in global_config:
            for admin_id in global_config["admins_id"]:
                if str(admin_id).isdigit():
                    self.admins.append(str(admin_id))

        logger.info(f"[Safety] 加载管理员列表: {self.admins}")

        # --- 初始化数据文件 ---
        if not DATA_DIR.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not DATA_FILE.exists():
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)

        # --- 启动后台监控任务 ---
        self.monitor_task = asyncio.create_task(self._monitor_loop())

    # ================= 工具方法 (增强版) =================

    def _load_users(self) -> dict:
        """读取用户数据，增加损坏自动修复功能"""
        if not DATA_FILE.exists():
            return {}

        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            # 如果文件损坏，自动备份并重置，防止插件卡死
            logger.error("[Safety] 数据文件 users.json 已损坏！正在备份并重置...")
            try:
                backup_path = DATA_FILE.with_suffix(f".bak.{int(time.time())}")
                copyfile(DATA_FILE, backup_path)
                logger.warning(f"[Safety] 已备份损坏文件至: {backup_path}")
            except Exception as e:
                logger.error(f"[Safety] 备份失败: {e}")

            return {}  # 返回空字典，重新开始
        except Exception as e:
            logger.error(f"[Safety] 读取用户数据未知失败: {e}")
            return {}

    def _save_users(self, data: dict):
        """保存数据"""
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except TypeError as e:
            logger.error(f"[Safety] 保存数据时发生类型错误(可能是混入了不可序列化对象): {e}")
            # 尝试通过简单的日志打印出有问题的数据，方便排查
            # logger.error(f"Data: {data}")
        except Exception as e:
            logger.error(f"[Safety] 保存数据失败: {e}")

    def _update_activity(self, user_id: str, group_id: str = None, bot_id: str = None):
        """更新用户活跃时间"""
        users = self._load_users()
        if user_id in users:
            users[user_id]["last_active"] = time.time()
            users[user_id]["alert_level"] = 0
            if group_id: users[user_id]["group_id"] = str(group_id)  # 强制转str
            if bot_id: users[user_id]["bot_id"] = str(bot_id)  # 强制转str
            self._save_users(users)
            return True
        return False

    def _format_time(self, timestamp):
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

    def _format_duration(self, seconds):
        days = int(seconds // 86400)
        remaining = seconds % 86400
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)

        parts = []
        if days > 0: parts.append(f"{days}天")
        if hours > 0: parts.append(f"{hours}小时")
        if minutes > 0: parts.append(f"{minutes}分")
        return "".join(parts) if parts else "少于1分钟"

    def _days_to_desc(self, days_float):
        total_minutes = int(days_float * 24 * 60)
        d = total_minutes // 1440
        h = (total_minutes % 1440) // 60
        m = total_minutes % 60
        desc = f"{days_float}天 ("
        if d > 0: desc += f"{d}天"
        if h > 0: desc += f"{h}小时"
        desc += f"{m}分钟)"
        return desc

    # ================= 管理员指令 =================

    @filter.command("安全监控列表")
    async def cmd_admin_check(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())  # 强制转str
        if sender_id not in self.admins:
            yield event.plain_result("❌ 权限不足，仅管理员可用。")
            return

        users = self._load_users()
        if not users:
            yield event.plain_result("📂 当前没有正在监控的用户。")
            return

        msg_lines = ["📋 [管理员] 全员安全监控报表", "----------------"]
        now = time.time()

        for uid, info in users.items():
            last_active = info.get("last_active", 0)
            diff = now - last_active
            level = info.get("alert_level", 0)
            max_days = float(info.get("max_missing_days", 3))
            contact = info.get("emergency_contact", "未设置")

            if level == 0:
                status = "🟢 正常"
            elif level == 1:
                status = "🟡 警告中"
            else:
                status = "🔴 已失联"

            line = (
                f"{status} 用户: {uid}\n"
                f"   ├ 失联时长: {self._format_duration(diff)}\n"
                f"   ├ 设定阈值: {max_days}天\n"
                f"   └ 紧急联系: {contact}"
            )
            msg_lines.append(line)

        yield event.plain_result("\n".join(msg_lines))

    # ================= 用户指令交互 =================

    @filter.command("注册又活一天")
    async def cmd_register(self, event: AstrMessageEvent):
        # 1. 强制类型转换，解决 partial 错误
        user_id = str(event.get_sender_id())

        # 处理 group_id，防止 None
        raw_group_id = event.get_group_id()
        group_id = str(raw_group_id) if raw_group_id else ""

        # 处理 bot_id，防止对象或属性异常
        bot_id = str(event.bot.id) if event.bot else "unknown"

        users = self._load_users()

        if user_id not in users:
            users[user_id] = {
                "user_id": user_id,
                "bot_id": bot_id,
                "group_id": group_id,
                "emergency_contact": "",
                "max_missing_days": 3.0,
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

    @filter.command("配置紧急联系人")
    async def cmd_set_contact(self, event: AstrMessageEvent, contact_qq: str):
        user_id = str(event.get_sender_id())
        users = self._load_users()

        if user_id not in users:
            yield event.plain_result("❌ 请先发送 /注册又活一天 开启功能。")
            return

        if not contact_qq.isdigit():
            yield event.plain_result("❌ 联系人必须是QQ号（纯数字）。")
            return

        users[user_id]["emergency_contact"] = str(contact_qq)
        self._save_users(users)
        yield event.plain_result(f"✅ 紧急联系人已设置为: {contact_qq}")

    @filter.command("设置失联时间")
    async def cmd_set_days(self, event: AstrMessageEvent, days: str):
        user_id = str(event.get_sender_id())
        users = self._load_users()

        if user_id not in users:
            yield event.plain_result("❌ 请先发送 /注册又活一天 开启功能。")
            return

        try:
            days_float = float(days)
            if days_float <= 0: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 输入无效。请输入数字，例如 3 或 0.5")
            return

        users[user_id]["max_missing_days"] = days_float
        self._save_users(users)
        yield event.plain_result(f"✅ 设置成功。若 {self._days_to_desc(days_float)} 无消息，将联系紧急联系人。")

    # ================= 被动监听 =================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent, *args):
        if not event: return
        user_id = str(event.get_sender_id())
        self._update_activity(user_id)

    # ================= 核心后台逻辑 =================

    async def _send_private_raw(self, bot, user_id, text):
        try:
            await bot.send_private_msg(
                user_id=int(user_id),
                message=[{"type": "text", "data": {"text": text}}]
            )
        except Exception as e:
            logger.error(f"[Safety] 私聊发送失败: {e}")

    async def _send_group_at_raw(self, bot, group_id, user_id, text):
        try:
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
        if not group_id: return False
        try:
            member = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
            return member is not None
        except:
            return False

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(self.check_interval)

            users = self._load_users()
            now = time.time()
            dirty = False

            for uid, info in users.items():
                last = info.get("last_active", now)
                diff = now - last
                level = info.get("alert_level", 0)
                max_days = float(info.get("max_missing_days", 3.0))
                max_seconds = max_days * 86400

                # 获取 Bot 实例
                bot_id = info.get("bot_id")
                bot = self.context.get_bot(bot_id)
                if not bot: continue

                warn_threshold = 86400

                # 阶段 1: 预警
                if max_seconds > warn_threshold:
                    if diff > warn_threshold and level < 1:
                        if info.get("group_id"):
                            await self._send_group_at_raw(bot, info["group_id"], uid,
                                                          "⚠️ 你已经24小时没说话了，还活着吗？请冒个泡！")
                        await self._send_private_raw(bot, uid,
                                                     "⚠️ [安全提醒] 你已经一天没说话了，请回复任意消息报平安。")
                        info["alert_level"] = 1
                        dirty = True

                # 阶段 2: 紧急
                if diff > max_seconds and level < 2:
                    contact_id = info.get("emergency_contact")
                    time_desc = self._format_duration(diff)

                    if contact_id:
                        msg_text = f"🚨 [紧急求助] 用户 {uid} 已失联 {time_desc} (超过设定阈值)！"
                        is_in_group = await self._check_user_in_group(bot, info["group_id"], contact_id)

                        if is_in_group:
                            await self._send_group_at_raw(bot, info["group_id"], contact_id,
                                                          f"警告：用户 {uid} 已失联，请尝试联系！")
                            await self._send_private_raw(bot, contact_id, msg_text + " (已在群内同步提醒)")
                        else:
                            await self._send_private_raw(bot, contact_id, msg_text + " (请尝试通过电话联系他)")
                    else:
                        await self._send_private_raw(bot, uid, "🚨 [最终警告] 已达到失联阈值，但未设置紧急联系人。")

                    info["alert_level"] = 2
                    dirty = True

            if dirty:
                self._save_users(users)