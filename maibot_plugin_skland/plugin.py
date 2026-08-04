"""
MaiBot Plugin - 森空岛签到

命令:
- /skdhelp
- /skdlogin <token>   私聊登录并立即签到
- /skdlogout          私聊登出
- /skd                私聊查看自己；群聊查看本群绑定用户状态
- /skdusers           查看用户统计
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
)

from .skland_api import SignInResult, SklandAPI

USERS_FILE = "users.json"
GROUPS_FILE = "groups.json"


class PluginSection(PluginConfigBase):
    """插件基础配置（MaiBot 要求必须有 [plugin] 节）。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本号")
    enabled: bool = Field(default=True, description="是否启用插件")


class SignSection(PluginConfigBase):
    """签到相关配置。"""

    __ui_label__ = "签到设置"
    __ui_icon__ = "event"
    __ui_order__ = 1

    auto_sign_enabled: bool = Field(default=True, description="开启后，每天定时为所有绑定用户签到")
    auto_sign_hour: int = Field(default=9, description="自动签到小时（0-23）")
    auto_sign_minute: int = Field(default=0, description="自动签到分钟（0-59）")
    auto_sign_delay: int = Field(default=10, description="用户间随机延迟上限（秒），降低风控风险")
    max_users: int = Field(default=10, description="最大绑定用户数，0 表示不限制")
    show_player_name: bool = Field(default=True, description="优先显示森空岛昵称，否则显示平台昵称")
    notify_on_auto_sign: bool = Field(default=True, description="自动签到后私聊推送结果")


class SklandPluginConfig(PluginConfigBase):
    """森空岛签到插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    sign: SignSection = Field(default_factory=SignSection)


class SklandPlugin(MaiBotPlugin):
    """森空岛自动签到插件。"""

    config_model = SklandPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self.api = SklandAPI(max_retries=3)
        self._auto_sign_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._users_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._ensure_data_files()
        if self._is_enabled() and self._sign_cfg().auto_sign_enabled:
            self._start_auto_sign_loop()
        self.ctx.logger.info("森空岛签到插件已加载")

    async def on_unload(self) -> None:
        self._stop_event.set()
        if self._auto_sign_task and not self._auto_sign_task.done():
            self._auto_sign_task.cancel()
            try:
                await self._auto_sign_task
            except asyncio.CancelledError:
                pass
        await self.api.close()
        self.ctx.logger.info("森空岛签到插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.ctx.logger.info("森空岛插件配置已更新: version=%s", version)
        self._stop_event.set()
        if self._auto_sign_task and not self._auto_sign_task.done():
            self._auto_sign_task.cancel()
            try:
                await self._auto_sign_task
            except asyncio.CancelledError:
                pass
        self._stop_event = asyncio.Event()
        if self._is_enabled() and self._sign_cfg().auto_sign_enabled:
            self._start_auto_sign_loop()

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        try:
            return bool(self.config.plugin.enabled)
        except Exception:
            raw = self.get_plugin_config_data()
            return bool(raw.get("plugin", {}).get("enabled", True))

    def _sign_cfg(self) -> SignSection:
        try:
            return self.config.sign
        except Exception:
            raw = self.get_plugin_config_data().get("sign", {})
            return SignSection(
                auto_sign_enabled=bool(raw.get("auto_sign_enabled", True)),
                auto_sign_hour=int(raw.get("auto_sign_hour", 9)),
                auto_sign_minute=int(raw.get("auto_sign_minute", 0)),
                auto_sign_delay=int(raw.get("auto_sign_delay", 10)),
                max_users=int(raw.get("max_users", 10)),
                show_player_name=bool(raw.get("show_player_name", True)),
                notify_on_auto_sign=bool(raw.get("notify_on_auto_sign", True)),
            )

    # ------------------------------------------------------------------
    # storage
    # ------------------------------------------------------------------

    def _data_path(self, name: str) -> Path:
        path = Path(self.ctx.paths.data_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _ensure_data_files(self) -> None:
        for name, default in ((USERS_FILE, {}), (GROUPS_FILE, {})):
            path = self._data_path(name)
            if not path.exists():
                path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_json(self, name: str) -> dict[str, Any]:
        path = self._data_path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_json(self, name: str, data: dict[str, Any]) -> None:
        path = self._data_path(name)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _get_users(self) -> dict[str, Any]:
        async with self._users_lock:
            return self._load_json(USERS_FILE)

    async def _put_users(self, users: dict[str, Any]) -> None:
        async with self._users_lock:
            self._save_json(USERS_FILE, users)

    async def _get_groups(self) -> dict[str, Any]:
        async with self._users_lock:
            return self._load_json(GROUPS_FILE)

    async def _put_groups(self, groups: dict[str, Any]) -> None:
        async with self._users_lock:
            self._save_json(GROUPS_FILE, groups)

    # ------------------------------------------------------------------
    # message helpers
    # ------------------------------------------------------------------

    def _extract_context(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """从 Command kwargs 中提取用户/群聊上下文。"""
        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        user_info = message.get("user_info") or {}
        chat_info = message.get("chat_info") or message.get("message_info") or {}
        group_info = chat_info.get("group_info") or message.get("group_info") or {}

        user_id = str(
            user_info.get("user_id")
            or message.get("user_id")
            or kwargs.get("user_id")
            or ""
        )
        user_name = str(
            user_info.get("user_nickname")
            or user_info.get("nickname")
            or message.get("user_nickname")
            or user_id
            or "未知"
        )
        group_id = str(
            group_info.get("group_id")
            or chat_info.get("group_id")
            or message.get("group_id")
            or ""
        )
        platform = str(
            message.get("platform")
            or chat_info.get("platform")
            or user_info.get("platform")
            or "qq"
        )
        stream_id = str(kwargs.get("stream_id") or message.get("stream_id") or "")
        is_group = bool(group_id) and group_id not in ("0", "None", "none")

        return {
            "user_id": user_id,
            "user_name": user_name,
            "group_id": group_id,
            "platform": platform,
            "stream_id": stream_id,
            "is_group": is_group,
            "message": message,
        }

    async def _reply(self, stream_id: str, text: str) -> None:
        if not stream_id:
            self.ctx.logger.warning("缺少 stream_id，无法发送消息: %s", text[:80])
            return
        await self.ctx.send.text(text, stream_id)

    def _is_signed_today(self, result: SignInResult) -> bool:
        if result.success:
            return True
        error = (result.error or "").lower()
        return any(k in error for k in ["已签到", "请勿重复", "重复签到", "already", "签到过", "今日已"])

    def _format_sign_status(self, results: list[SignInResult], nickname: str = "") -> str:
        if not results:
            return "没有绑定游戏"
        lines: list[str] = []
        if nickname:
            lines.append(f"【{nickname}】")
        for r in results:
            if r.success or self._is_signed_today(r):
                award = ", ".join(r.awards) if r.awards else "无奖励"
                lines.append(f"{r.game} 已签到 ({award})")
            else:
                lines.append(f"{r.game} 签到失败: {r.error}")
        return "\n".join(lines)

    def _update_last_sign(self, user_data: dict[str, Any], results: list[SignInResult]) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        last_sign = user_data.setdefault("last_sign", {})
        for r in results:
            if r.game == "明日方舟" and self._is_signed_today(r):
                last_sign["arknights"] = today
            elif r.game == "终末地" and self._is_signed_today(r):
                last_sign["endfield"] = today

    def _display_name(self, user_data: dict[str, Any], nickname: str | None = None) -> str:
        if self._sign_cfg().show_player_name:
            name = (nickname or user_data.get("nickname") or "").strip()
            if name:
                return name
        return (user_data.get("last_username") or user_data.get("nickname") or "未知").strip() or "未知"

    async def _bind_user_to_group(self, group_id: str, user_id: str) -> None:
        if not group_id or not user_id:
            return
        groups = await self._get_groups()
        members = groups.setdefault(group_id, [])
        if user_id not in members:
            members.append(user_id)
            await self._put_groups(groups)

    async def _send_private_by_user(self, user_id: str, user_data: dict[str, Any], text: str) -> None:
        stream_id = user_data.get("stream_id") or ""
        if stream_id:
            try:
                await self.ctx.send.text(text, stream_id)
                return
            except Exception as e:
                self.ctx.logger.warning("使用缓存 stream_id 私聊失败: %s", e)

        platform = user_data.get("platform") or "qq"
        try:
            stream = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="private",
                user_id=user_id,
            )
            target = ""
            if isinstance(stream, dict):
                target = str(stream.get("stream_id") or "")
            elif isinstance(stream, str):
                target = stream
            if target:
                await self.ctx.send.text(text, target)
                return
        except Exception as e:
            self.ctx.logger.error("打开私聊会话失败 user=%s: %s", user_id, e)

        self.ctx.logger.warning("无法向用户 %s 发送私聊消息", user_id)

    # ------------------------------------------------------------------
    # auto sign
    # ------------------------------------------------------------------

    def _start_auto_sign_loop(self) -> None:
        self._auto_sign_task = asyncio.create_task(self._auto_sign_loop(), name="skland-auto-sign")
        cfg = self._sign_cfg()
        self.ctx.logger.info(
            "森空岛自动签到循环已启动，计划每天 %02d:%02d 执行",
            max(0, min(23, cfg.auto_sign_hour)),
            max(0, min(59, cfg.auto_sign_minute)),
        )

    def _seconds_until_next_run(self) -> float:
        cfg = self._sign_cfg()
        hour = max(0, min(23, int(cfg.auto_sign_hour)))
        minute = max(0, min(59, int(cfg.auto_sign_minute)))
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    async def _auto_sign_loop(self) -> None:
        while not self._stop_event.is_set():
            wait_seconds = self._seconds_until_next_run()
            self.ctx.logger.info("距离下次自动签到还有 %.0f 秒", wait_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                break
            except asyncio.TimeoutError:
                pass

            if self._stop_event.is_set():
                break
            if not self._is_enabled() or not self._sign_cfg().auto_sign_enabled:
                self.ctx.logger.info("自动签到已关闭，跳过本轮")
                continue

            try:
                await self._auto_sign_all_users()
            except Exception as e:
                self.ctx.logger.error("自动签到执行异常: %s", e, exc_info=True)

    async def _auto_sign_all_users(self) -> None:
        cfg = self._sign_cfg()
        users = await self._get_users()
        if not users:
            self.ctx.logger.info("没有已绑定用户，跳过自动签到")
            return

        self.ctx.logger.info("开始自动签到，共 %d 名用户", len(users))
        max_delay = max(0, int(cfg.auto_sign_delay))

        for user_id, user_data in list(users.items()):
            if max_delay > 0:
                delay = random.uniform(0, max_delay)
                await asyncio.sleep(delay)

            token = user_data.get("token")
            if not token:
                continue

            try:
                results, nickname = await self.api.do_full_sign_in(token)
                if nickname:
                    user_data["nickname"] = nickname
                self._update_last_sign(user_data, results)
                users[user_id] = user_data

                message = f"🎮 森空岛自动签到结果\n\n{self._format_sign_status(results, nickname)}"
                if cfg.notify_on_auto_sign:
                    await self._send_private_by_user(user_id, user_data, message)
                self.ctx.logger.info("用户 %s (%s) 自动签到完成", user_id, nickname)
            except Exception as e:
                self.ctx.logger.error("用户 %s 自动签到失败: %s", user_id, e)
                if cfg.notify_on_auto_sign:
                    await self._send_private_by_user(
                        user_id,
                        user_data,
                        f"⚠️ 自动签到失败\n错误: {e}\n请使用 /skdlogin 重新登录",
                    )

        await self._put_users(users)
        self.ctx.logger.info("自动签到执行完毕")

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @Command("skdhelp", description="森空岛签到帮助", pattern=r"^/skdhelp$")
    async def skdhelp(self, **kwargs):
        stream_id = kwargs.get("stream_id", "")
        text = (
            "森空岛签到插件帮助\n"
            "1. 私聊发送 /skdlogin <token> 登录并签到\n"
            "2. 私聊发送 /skdlogout 登出\n"
            "3. /skd 查看签到状态（群聊显示群内绑定用户）\n"
            "4. /skdusers 查看绑定人数\n\n"
            "Token 获取：\n"
            "登录鹰角通行证后打开 https://web-api.hypergryph.com/account/info/hg\n"
            "复制 JSON 中 content 字段的值"
        )
        await self._reply(stream_id, text)
        return True, "帮助已发送", 2

    @Command(
        "skdlogin",
        description="使用森空岛 token 登录并签到（仅私聊）",
        pattern=r"^/skdlogin(?:\s+(?P<token>\S+))?$",
    )
    async def skdlogin(self, **kwargs):
        if not self._is_enabled():
            await self._reply(kwargs.get("stream_id", ""), "插件已禁用")
            return False, "插件已禁用", 1

        ctx = self._extract_context(kwargs)
        stream_id = ctx["stream_id"]
        matched = kwargs.get("matched_groups") or {}
        token = str(matched.get("token") or "").strip()

        if ctx["is_group"]:
            await self._reply(stream_id, "请在私聊中使用此命令登录\n为保护隐私，请将群内登录消息撤回")
            return False, "仅私聊可用", 2

        if not token:
            tip = (
                "请先获取 token：\n"
                "1. 登录鹰角网络通行证后打开 https://web-api.hypergryph.com/account/info/hg\n"
                "   或登录森空岛后打开 https://web-api.skland.com/account/info/hg\n"
                "2. 复制 JSON 中 content 字段的值\n"
                "3. 私聊发送：/skdlogin <content>"
            )
            await self._reply(stream_id, tip)
            return False, "缺少 token", 1

        if not ctx["user_id"]:
            await self._reply(stream_id, "无法识别用户身份，登录失败")
            return False, "缺少 user_id", 1

        users = await self._get_users()
        max_users = int(self._sign_cfg().max_users)
        if ctx["user_id"] not in users and max_users > 0 and len(users) >= max_users:
            await self._reply(stream_id, f"❌ 绑定失败：已达到最大用户数限制（{max_users}个）")
            return False, "超过人数限制", 1

        await self._reply(stream_id, "正在登录并签到，请稍候...")
        try:
            results, nickname = await self.api.do_full_sign_in(token)
            user_data = {
                "token": token,
                "nickname": nickname,
                "last_username": ctx["user_name"],
                "last_sign": {},
                "bound_at": datetime.now().isoformat(),
                "platform": ctx["platform"],
                "stream_id": stream_id,
            }
            self._update_last_sign(user_data, results)
            users[ctx["user_id"]] = user_data
            await self._put_users(users)

            text = f"登录成功！\n{self._format_sign_status(results, nickname)}"
            await self._reply(stream_id, text)
            return True, "登录成功", 2
        except Exception as e:
            self.ctx.logger.error("skdlogin 失败: %s", e, exc_info=True)
            await self._reply(stream_id, f"登录失败: {e}")
            return False, f"登录失败: {e}", 1

    @Command("skdlogout", description="登出并移除 token（仅私聊）", pattern=r"^/skdlogout$")
    async def skdlogout(self, **kwargs):
        ctx = self._extract_context(kwargs)
        stream_id = ctx["stream_id"]

        if ctx["is_group"]:
            await self._reply(stream_id, "请在私聊中使用此命令登出")
            return False, "仅私聊可用", 2

        users = await self._get_users()
        if ctx["user_id"] in users:
            del users[ctx["user_id"]]
            await self._put_users(users)

            # 从所有群映射中移除
            groups = await self._get_groups()
            changed = False
            for gid, members in list(groups.items()):
                if ctx["user_id"] in members:
                    members = [m for m in members if m != ctx["user_id"]]
                    if members:
                        groups[gid] = members
                    else:
                        del groups[gid]
                    changed = True
            if changed:
                await self._put_groups(groups)

            await self._reply(stream_id, "已退出登录并清除绑定信息")
            return True, "已登出", 2

        await self._reply(stream_id, "您尚未绑定森空岛账号")
        return False, "未绑定", 1

    @Command("skdusers", description="查看绑定用户统计", pattern=r"^/skdusers$")
    async def skdusers(self, **kwargs):
        users = await self._get_users()
        max_users = int(self._sign_cfg().max_users)
        signed = sum(1 for u in users.values() if u.get("last_sign"))

        lines = [
            "📊 森空岛签到用户统计",
            "═══════════════════",
            f"📝 总注册用户: {len(users)} 人",
            f"📉 无签到记录: {len(users) - signed} 人",
        ]
        if max_users > 0:
            remaining = max(0, max_users - len(users))
            lines.append(f"🎯 最大限制: {max_users} 人")
            lines.append(f"🆓 剩余名额: {remaining} 人")

        text = "\n".join(lines)
        await self._reply(kwargs.get("stream_id", ""), text)
        return True, "统计已发送", 1

    @Command("skd", description="查看/执行森空岛签到", pattern=r"^/skd$")
    async def skd(self, **kwargs):
        if not self._is_enabled():
            await self._reply(kwargs.get("stream_id", ""), "插件已禁用")
            return False, "插件已禁用", 1

        ctx = self._extract_context(kwargs)
        stream_id = ctx["stream_id"]
        users = await self._get_users()

        if ctx["is_group"]:
            if ctx["user_id"] in users:
                await self._bind_user_to_group(ctx["group_id"], ctx["user_id"])

            groups = await self._get_groups()
            group_users = groups.get(ctx["group_id"], [])
            lines = [
                "森空岛签到统计",
                "═══════════════",
                "方舟 | 终末 | 昵称",
                "-----------------",
            ]

            if not group_users:
                lines.append("本群暂无绑定用户。绑定用户在群内发送 /skd 会自动加入本群列表。")
                await self._reply(stream_id, "\n".join(lines))
                return True, "群统计为空", 1

            for uid in list(group_users):
                user_data = users.get(uid)
                if not user_data or not user_data.get("token"):
                    continue
                try:
                    results, nickname = await self.api.do_full_sign_in(user_data["token"])
                    if nickname:
                        user_data["nickname"] = nickname
                    if uid == ctx["user_id"] and ctx["user_name"]:
                        user_data["last_username"] = ctx["user_name"]
                    self._update_last_sign(user_data, results)
                    users[uid] = user_data

                    ak = "✅" if user_data.get("last_sign", {}).get("arknights") else "❌"
                    ef = "✅" if user_data.get("last_sign", {}).get("endfield") else "❌"
                    name = self._display_name(user_data, nickname)
                    lines.append(f" {ak} | {ef} | {name}")
                except Exception as e:
                    self.ctx.logger.error("群签到失败 user=%s: %s", uid, e)
                    lines.append(" ⚠️ | ⚠️ | (Error)")

            await self._put_users(users)
            await self._reply(stream_id, "\n".join(lines))
            return True, "群统计已发送", 1

        # 私聊
        user_data = users.get(ctx["user_id"])
        if not user_data:
            await self._reply(stream_id, "你还未绑定账号，请使用 /skdlogin <token>")
            return False, "未绑定", 1

        try:
            results, nickname = await self.api.do_full_sign_in(user_data["token"])
            if nickname:
                user_data["nickname"] = nickname
            user_data["stream_id"] = stream_id
            user_data["last_username"] = ctx["user_name"] or user_data.get("last_username")
            self._update_last_sign(user_data, results)
            users[ctx["user_id"]] = user_data
            await self._put_users(users)

            text = self._format_sign_status(results, nickname)
            await self._reply(stream_id, text)
            return True, "签到完成", 1
        except Exception as e:
            self.ctx.logger.error("skd 查询失败: %s", e, exc_info=True)
            await self._reply(stream_id, f"查询失败: {e}")
            return False, f"查询失败: {e}", 1


def create_plugin():
    return SklandPlugin()
