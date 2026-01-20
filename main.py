import os
import json
import time
import asyncio
import smtplib
import ssl
from datetime import datetime
from pathlib import Path
from shutil import copyfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr, make_msgid, formatdate

from astrbot.api.all import *
from astrbot.api.event import filter
from astrbot.api import logger
from .sign_system import SignSystem


@register("astrbot_plugin_safety", "shskjw", "噢耶，今天又活一天", "1.0.8")
class SafetyPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.check_interval = config.get("check_interval", 3600)

        # --- 初始化目录和文件 ---
        self.data_dir = Path(os.getcwd()) / "data" / "plugin_data" / "astrbot_plugin_safety"
        self.data_file = self.data_dir / "users.json"

        # --- 内存缓存 ---
        self.cache = {}
        self.is_dirty = False

        # --- Bot 实例缓存池 ---
        self.connected_bots = {}

        # --- 加载管理员 ---
        self.admins = []
        global_config = context.get_config()
        if global_config and "admins_id" in global_config:
            for admin_id in global_config["admins_id"]:
                if str(admin_id).isdigit():
                    self.admins.append(str(admin_id))

        # 自动创建文件夹
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        self.sign_system = SignSystem(self.data_dir)

        self._sync_init_load()

        # --- 启动后台监控 ---
        self.monitor_task = asyncio.create_task(self._monitor_loop())

    @command("打卡")
    async def sign_in_command(self, event: AstrMessageEvent):
        """每日打卡"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        # 1. 执行打卡
        success, msg = self.sign_system.sign_in(user_id)
        
        # 2. 生成图片
        image = await self.sign_system.draw_calendar_image(user_id)
        
        # 3. 保存临时文件
        temp_img_path = self.data_dir / f"temp_sign_{user_id}.png"
        image.save(temp_img_path)
        
        yield event.plain_result(f"{msg}")
        yield event.image_result(str(temp_img_path))

    @command("补签")
    async def cmd_supplement_sign(self, event: AstrMessageEvent):
        """补签(最近两天)"""
        user_id = event.get_sender_id()
        
        # 解析参数
        raw_msg = event.message_str or ""
        parts = raw_msg.split(maxsplit=1)
        date_str = parts[1].strip() if len(parts) > 1 else None
        
        success, msg = self.sign_system.supplement_sign_in(user_id, date_str)
        
        if success:
            # 补签成功后发送日历
            image = await self.sign_system.draw_calendar_image(user_id)
            temp_img_path = self.data_dir / f"temp_sign_{user_id}.png"
            image.save(temp_img_path)
            yield event.plain_result(msg)
            yield event.image_result(str(temp_img_path))
        else:
            yield event.plain_result(f"❌ {msg}")

    # ================= 核心：Bot 收集 =================
    def _record_bot(self, bot):
        if bot and hasattr(bot, 'id'):
            self.connected_bots[str(bot.id)] = bot

    def _get_bot_instance(self, bot_id: str):
        if bot_id in self.connected_bots:
            return self.connected_bots[bot_id]
        if len(self.connected_bots) == 1:
            return list(self.connected_bots.values())[0]
        return None

    # ================= 核心：数据 I/O =================
    def _sync_init_load(self):
        """同步加载数据"""
        if not self.data_file.exists():
            self._init_empty_file()
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                self.cache = json.load(f)
        except Exception as e:
            logger.error(f"[Safety] 数据文件读取失败: {e}")
            self._backup_and_reset()

    def _init_empty_file(self):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump({}, f)
        self.cache = {}

    def _backup_and_reset(self):
        try:
            timestamp = int(time.time())
            backup_path = self.data_file.with_suffix(f".bak.{timestamp}")
            if self.data_file.exists():
                copyfile(self.data_file, backup_path)
        except Exception:
            pass
        self._init_empty_file()

    async def _async_save_users(self):
        if not self.cache: return
        data_to_save = self.cache.copy()
        try:
            await asyncio.to_thread(self._thread_write_task, data_to_save)
            self.is_dirty = False
        except Exception as e:
            logger.error(f"[Safety] 保存失败: {e}")

    def _thread_write_task(self, data):
        """线程写入任务"""
        temp_file = self.data_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_file, self.data_file)
        except Exception as e:
            logger.error(f"[Safety] 写入失败: {e}")

    # ================= 核心：邮件发送模块 =================

    def _get_target_email(self, info: dict):
        custom_email = info.get("email")
        if custom_email and "@" in custom_email:
            return custom_email
        contact_qq = info.get("emergency_contact")
        if contact_qq and contact_qq.isdigit():
            return f"{contact_qq}@qq.com"
        return None

    async def _async_send_email(self, user_info: dict, subject: str, body: str):
        target_email = self._get_target_email(user_info)
        if not target_email: return

        smtp_conf = user_info.get("smtp_override", {})
        host = smtp_conf.get("host", self.config.get("smtp_host", "smtpdm.aliyun.com"))
        port = int(smtp_conf.get("port", self.config.get("smtp_port", 465)))
        user = smtp_conf.get("user", self.config.get("smtp_user", "are-you-still-alive@x.mizhoubaobei.top"))
        password = smtp_conf.get("pass", self.config.get("smtp_pass", "ZM13199@%"))

        try:
            await asyncio.to_thread(self._thread_send_email, host, port, user, password, target_email, subject, body)
            logger.info(f"[Safety] 邮件已发送至 {target_email}")
        except smtplib.SMTPAuthenticationError:
            logger.error(f"[Safety] 邮件认证失败！请检查配置账号({user})和密码。")
        except Exception as e:
            logger.error(f"[Safety] 邮件发送失败 ({target_email}): {e}")

    def _thread_send_email(self, host, port, user, password, to_addr, subject, body):
        msg = MIMEMultipart('alternative')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = formataddr(["防失联卫士", user])
        msg['To'] = to_addr
        msg['Reply-to'] = user
        msg['Message-id'] = make_msgid()
        msg['Date'] = formatdate()

        text_part = MIMEText(body, 'plain', 'utf-8')
        msg.attach(text_part)

        try:
            if port == 465:
                context = ssl.create_default_context()
                context.set_ciphers('DEFAULT')
                client = smtplib.SMTP_SSL(host, port, context=context)
            else:
                client = smtplib.SMTP(host, port)

            client.login(user, password)
            client.sendmail(user, [to_addr], msg.as_string())
            client.quit()
        except Exception as e:
            raise e

    # ================= 业务逻辑 =================

    def _update_activity_memory(self, user_id: str, group_id: str = None, bot_id: str = None):
        user_id = str(user_id)
        if user_id in self.cache:
            if group_id: self.cache[user_id]["group_id"] = str(group_id)
            if bot_id: self.cache[user_id]["bot_id"] = str(bot_id)
            self.cache[user_id]["last_active"] = time.time()
            self.cache[user_id]["alert_level"] = 0
            self.is_dirty = True
            return True
        return False

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
        return f"{days_float}天 ({d}天{h}小时{m}分)"

    def _get_msg_content(self, info: dict, msg_type: str, default_text: str):
        custom = ""
        if msg_type == "warn":
            custom = info.get("custom_warn_msg", "")
        elif msg_type == "emerg":
            custom = info.get("custom_emerg_msg", "")
        if custom and custom.strip():
            return custom
        return default_text

    # ================= 用户指令 =================

    @command("设置一阶段")
    async def cmd_set_warn_msg(self, event: AstrMessageEvent):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        user_id = str(event.get_sender_id())

        if user_id not in self.cache:
            yield event.plain_result("❌ 请先发送 /注册又活一天")
            return

        # 手动解析参数
        raw_msg = event.message_str or ""
        parts = raw_msg.split(maxsplit=1)
        # parts[0] 是指令，parts[1] 是内容，如果存在
        message = parts[1].strip() if len(parts) > 1 else ""

        if not message:
            current = self.cache[user_id].get("custom_warn_msg", "（默认）")
            if not current: current = "（默认）"
            yield event.plain_result(f"📝 当前一阶段(预警)话术：\n{current}\n\n如需修改，请在指令后加上新话术。")
            return

        self.cache[user_id]["custom_warn_msg"] = message
        await self._async_save_users()
        yield event.plain_result(f"✅ 一阶段预警话术已更新！")

    @command("设置二阶段")
    async def cmd_set_emerg_msg(self, event: AstrMessageEvent):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        user_id = str(event.get_sender_id())

        if user_id not in self.cache:
            yield event.plain_result("❌ 请先发送 /注册又活一天")
            return

        # 手动解析参数
        raw_msg = event.message_str or ""
        parts = raw_msg.split(maxsplit=1)
        message = parts[1].strip() if len(parts) > 1 else ""

        if not message:
            current = self.cache[user_id].get("custom_emerg_msg", "（默认）")
            if not current: current = "（默认）"
            yield event.plain_result(f"📝 当前二阶段(报警)话术：\n{current}\n\n如需修改，请在指令后加上新话术。")
            return

        self.cache[user_id]["custom_emerg_msg"] = message
        await self._async_save_users()
        yield event.plain_result(f"✅ 二阶段报警话术已更新！")

    @filter.command("绑定邮箱")
    async def cmd_bind_email(self, event: AstrMessageEvent, email: str = None):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        user_id = str(event.get_sender_id())

        if not email:
            yield event.plain_result("❌ 请输入邮箱地址。\n示例：/绑定邮箱 123@qq.com")
            return

        email = str(email)  # 强制转字符串

        if user_id not in self.cache:
            yield event.plain_result("❌ 请先发送 /注册又活一天")
            return

        if "@" not in email or "." not in email:
            yield event.plain_result("❌ 邮箱格式不正确。")
            return

        self.cache[user_id]["email"] = email
        await self._async_save_users()

        asyncio.create_task(self._async_send_email(
            self.cache[user_id],
            "【防失联卫士】邮箱绑定测试",
            f"您好，用户 {user_id} 已成功绑定此邮箱。"
        ))

        yield event.plain_result(f"✅ 邮箱已绑定: {email}\n优先发送到此邮箱，若未绑定则自动发给紧急联系人QQ邮箱。")

    @filter.command("注册又活一天")
    async def cmd_register(self, event: AstrMessageEvent):
        if hasattr(event, 'bot'): self._record_bot(event.bot)

        user_id = str(event.get_sender_id())
        raw_group_id = event.get_group_id()
        group_id = str(raw_group_id) if raw_group_id else ""
        bot_id = str(event.bot.id) if (hasattr(event, 'bot') and event.bot) else "unknown"

        if user_id not in self.cache:
            self.cache[user_id] = {
                "user_id": user_id,
                "bot_id": bot_id,
                "group_id": group_id,
                "emergency_contact": "",
                "email": "",
                "max_missing_days": 3.0,
                "last_active": time.time(),
                "alert_level": 0,
                "custom_warn_msg": "",
                "custom_emerg_msg": ""
            }
            msg = "✅ 注册成功！\n请发送 /配置紧急联系人 [QQ号]\n(可选) /绑定邮箱"
        else:
            self.cache[user_id]["last_active"] = time.time()
            self.cache[user_id]["alert_level"] = 0
            self.cache[user_id]["bot_id"] = bot_id
            if group_id: self.cache[user_id]["group_id"] = group_id
            msg = "✅ 打卡成功！计时器已重置。"

        await self._async_save_users()
        yield event.plain_result(msg)

    @filter.command("配置紧急联系人")
    async def cmd_set_contact(self, event: AstrMessageEvent, contact_qq: str = None):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        user_id = str(event.get_sender_id())

        if not contact_qq:
            yield event.plain_result("❌ 请输入QQ号。\n示例：/配置紧急联系人 12345678")
            return

        contact_qq = str(contact_qq)

        if user_id not in self.cache:
            yield event.plain_result("❌ 请先发送 /注册又活一天")
            return

        if not contact_qq.isdigit():
            yield event.plain_result("❌ QQ号必须是纯数字")
            return

        self.cache[user_id]["emergency_contact"] = contact_qq
        await self._async_save_users()
        yield event.plain_result(f"✅ 紧急联系人已更新")

    @filter.command("设置失联时间")
    async def cmd_set_days(self, event: AstrMessageEvent, days: str = None):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        user_id = str(event.get_sender_id())

        if not days:
            yield event.plain_result("❌ 请输入天数。\n示例：/设置失联时间 3")
            return

        if user_id not in self.cache:
            yield event.plain_result("❌ 请先发送 /注册又活一天")
            return
        try:
            # 兼容处理
            days_float = float(str(days))
            if days_float <= 0: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请输入有效数字")
            return
        self.cache[user_id]["max_missing_days"] = days_float
        await self._async_save_users()
        yield event.plain_result(f"✅ 设置成功。阈值: {self._days_to_desc(days_float)}")

    # ================= 管理员指令 =================

    @filter.command("重载安全配置")
    async def cmd_reload_config(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        if sender_id not in self.admins:
            yield event.plain_result("❌ 权限不足。")
            return

        await asyncio.to_thread(self._sync_init_load)
        yield event.plain_result(f"✅ 配置文件已重载！当前缓存 {len(self.cache)} 个用户。")

    @filter.command("安全监控列表")
    async def cmd_admin_check(self, event: AstrMessageEvent):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        sender_id = str(event.get_sender_id())
        if sender_id not in self.admins:
            yield event.plain_result("❌ 权限不足。")
            return

        msg_lines = ["📋 [管理员] 全员安全监控报表", "----------------"]
        now = time.time()

        for uid, info in self.cache.items():
            diff = now - info.get("last_active", 0)
            level = info.get("alert_level", 0)
            target_mail = self._get_target_email(info) or "无"

            if level == 0:
                status = "🟢 正常"
            elif level == 1:
                status = "🟡 警告"
            else:
                status = "🔴 失联"

            line = (
                f"{status} 用户: {uid}\n"
                f"   ├ 失联: {self._format_duration(diff)}\n"
                f"   ├ 邮箱: {target_mail}\n"
                f"   └ 话术: {'✏️' if info.get('custom_warn_msg') or info.get('custom_emerg_msg') else ''}"
            )
            msg_lines.append(line)

        yield event.plain_result("\n".join(msg_lines))

    # --- 测试指令 ---
    @filter.command("发送测试")
    async def cmd_admin_test(self, event: AstrMessageEvent, target_qq: str = None):
        if hasattr(event, 'bot'): self._record_bot(event.bot)
        sender_id = str(event.get_sender_id())
        if sender_id not in self.admins:
            yield event.plain_result("❌ 权限不足。")
            return

        target_id = str(target_qq) if target_qq else sender_id

        if target_id not in self.cache:
            yield event.plain_result(f"❌ 用户 {target_id} 未注册。")
            return

        info = self.cache[target_id]
        bot = getattr(event, 'bot', None)
        if not bot: bot = self._get_bot_instance(info.get("bot_id"))

        yield event.plain_result(f"🚀 开始全通道测试 (用户 {target_id})...")

        msg_text = self._get_msg_content(info, "emerg", f"🚨 [测试] 用户 {target_id} 正在测试失联报警。")

        target_email = self._get_target_email(info)
        if target_email:
            asyncio.create_task(self._async_send_email(
                info, "【恭喜又活一天】报警系统测试", f"测试邮件。\n报警内容：{msg_text}"
            ))
            yield event.plain_result(f"📧 邮件已发送 -> {target_email}")
        else:
            yield event.plain_result(f"⚠️ 无有效邮箱，跳过邮件发送。")

        if not bot:
            yield event.plain_result("❌ 找不到Bot，无法发送QQ消息。")
            return

        await self._send_private_raw(bot, target_id, msg_text + "\n(测试：发给用户)")
        yield event.plain_result(f"✅ 私聊已发送 -> 用户本人")

        contact_id = info.get("emergency_contact")
        group_id = info.get("group_id")

        if contact_id:
            await self._send_private_raw(bot, contact_id, msg_text + "\n(测试：发给联系人)")
            yield event.plain_result(f"✅ 私聊已发送 -> 紧急联系人")

            if group_id:
                chain = [
                    {"type": "at", "data": {"qq": target_id}},
                    {"type": "text", "data": {"text": " "}},
                    {"type": "at", "data": {"qq": contact_id}},
                    {"type": "text", "data": {"text": f" {msg_text}"}}
                ]
                try:
                    await bot.send_group_msg(group_id=int(group_id), message=chain)
                    yield event.plain_result(f"✅ 群聊已发送 -> @用户 @联系人")
                except Exception as e:
                    logger.error(f"[Safety] 测试群发失败: {e}")
                    yield event.plain_result(f"❌ 群聊发送失败")
        else:
            yield event.plain_result(f"⚠️ 未设置紧急联系人，跳过联系人相关测试。")

    # ================= 被动监听 =================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        # 修复参数不匹配报错：只接收 event，不接收 *args
        if not event or not hasattr(event, 'bot') or event.bot is None:
            return
        try:
            self._record_bot(event.bot)
            user_id = str(event.get_sender_id())
            self._update_activity_memory(user_id)
        except Exception:
            pass

    # ================= 后台任务 =================

    async def _send_private_raw(self, bot, user_id, text):
        try:
            await bot.send_private_msg(
                user_id=int(user_id),
                message=[{"type": "text", "data": {"text": text}}]
            )
            logger.info(f"[Safety] 私聊发送成功 -> {user_id}")
        except Exception as e:
            logger.error(f"[Safety] 私聊失败: {e}")

    async def _send_group_at_raw(self, bot, group_id, user_id, text):
        try:
            await bot.send_group_msg(
                group_id=int(group_id),
                message=[
                    {"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": f" {text}"}}
                ]
            )
            logger.info(f"[Safety] 群聊发送成功 -> 群{group_id} @{user_id}")
        except Exception as e:
            logger.error(f"[Safety] 群聊失败: {e}")

    async def _check_user_in_group(self, bot, group_id, user_id):
        if not group_id: return False
        try:
            m = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
            return m is not None
        except:
            return False

    async def _monitor_loop(self):
        logger.info(f"[Safety] 监控启动，周期: {self.check_interval}s")
        while True:
            await asyncio.sleep(self.check_interval)

            if self.is_dirty:
                await self._async_save_users()

            now = time.time()
            data_changed = False

            for uid in list(self.cache.keys()):
                info = self.cache[uid]
                last = info.get("last_active", now)
                diff = now - last
                level = info.get("alert_level", 0)
                max_days = float(info.get("max_missing_days", 3.0))
                max_seconds = max_days * 86400

                bot = self._get_bot_instance(info.get("bot_id"))
                warn_threshold = 86400

                # --- 阶段 1: 预警 ---
                if max_seconds > warn_threshold:
                    if diff > warn_threshold and level < 1:
                        default_warn = self.config.get("default_warn_msg",
                                                       "⚠️ [安全提醒] 你已24小时没冒泡了，请回复消息报平安。")
                        msg_text = self._get_msg_content(info, "warn", default_warn)

                        if bot:
                            if info.get("group_id"):
                                await self._send_group_at_raw(bot, info["group_id"], uid, msg_text)
                            await self._send_private_raw(bot, uid, msg_text)

                        if self._get_target_email(info):
                            await self._async_send_email(info, "【防失联卫士】日常活跃提醒", msg_text)

                        info["alert_level"] = 1
                        data_changed = True

                # --- 阶段 2: 紧急 ---
                if diff > max_seconds and level < 2:
                    logger.info(f"[Safety] 触发紧急报警 -> 用户 {uid}")
                    contact_id = info.get("emergency_contact")
                    time_desc = self._format_duration(diff)

                    default_emerg = self.config.get("default_emerg_msg",
                                                    "🚨 [紧急求助] 用户 {uid} 已失联 {time}，请尝试联系！")
                    raw_msg = self._get_msg_content(info, "emerg", default_emerg)
                    msg_text = raw_msg.replace("{uid}", uid).replace("{time}", time_desc)

                    if self._get_target_email(info):
                        await self._async_send_email(info, f"【紧急】用户 {uid} 失联警报",
                                                     f"系统检测到用户已失联 {time_desc}。\n\n报警内容：\n{msg_text}")

                    if bot:
                        await self._send_private_raw(bot, uid, msg_text + "\n(已触发紧急联系流程)")

                        if contact_id:
                            is_in_group = await self._check_user_in_group(bot, info.get("group_id"), contact_id)
                            # 私聊
                            await self._send_private_raw(bot, contact_id, msg_text + "\n(已在群内同步提醒)")
                            # 群聊
                            if is_in_group:
                                await self._send_group_at_raw(bot, info["group_id"], contact_id, msg_text)
                        else:
                            await self._send_private_raw(bot, uid, "🚨 [最终警告] 你未设置紧急联系人。")

                    info["alert_level"] = 2
                    data_changed = True

            if data_changed:
                await self._async_save_users()