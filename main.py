import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .box.config import PluginConfig as BoxConfig
from .box.service import BoxResult, BoxService

PLUGIN_NAME = "astrbot_plugin_join_review"

PENDING_TTL = 24 * 3600
DEDUP_TTL = 10

APPROVE = "approve"
REJECT = "reject"
BLACKLIST = "blacklist"

COMMANDS: dict[str, str] = {
    "同意": APPROVE,
    "通过": APPROVE,
    "批准": APPROVE,
    "拒绝": REJECT,
    "驳回": REJECT,
    "不同意": REJECT,
    "拉黑": BLACKLIST,
    "黑名单": BLACKLIST,
}

COMMAND_RE = re.compile(r"^(同意|通过|批准|拒绝|驳回|不同意|拉黑|黑名单)(?:\s+(@?\S+))?$")

_seen: dict[tuple, float] = {}


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    out: dict[str, Any] = {}
    for key in (
        "post_type",
        "notice_type",
        "request_type",
        "sub_type",
        "group_id",
        "user_id",
        "operator_id",
        "self_id",
        "flag",
        "comment",
    ):
        value = getattr(raw, key, None)
        if value is not None:
            out[key] = value
    return out


def _fmt(template: str, **kwargs: Any) -> str:
    def repl(match: re.Match) -> str:
        value = kwargs.get(match.group(1))
        return str(value) if value is not None else match.group(0)

    return re.sub(r"\{(\w+)\}", repl, str(template or ""))


class JoinReviewPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.blacklist_path = data_dir / "blacklist.json"
        self.pending_path = data_dir / "pending.json"
        self.blacklist = self._load(self.blacklist_path, {})
        self.pending = self._load(self.pending_path, {})

        self.box_cfg = BoxConfig(config, context)
        self.box = BoxService(self.box_cfg)

    # ------------------------------------------------------------------
    # 一、加群申请：先审核
    # ------------------------------------------------------------------
    @event_message_type(EventMessageType.GROUP_MESSAGE, priority=999)
    async def on_group_request(self, event: AstrMessageEvent):
        if not self._on("enable"):
            return
        raw = _as_dict(getattr(event.message_obj, "raw_message", None))
        if raw.get("post_type") != "request" or raw.get("request_type") != "group":
            return
        if raw.get("sub_type") != "add":
            return

        group_id = str(raw.get("group_id") or "").strip()
        user_id = str(raw.get("user_id") or "").strip()
        if not group_id or not user_id or user_id == str(event.get_self_id()):
            return
        if not self._group_enabled(group_id):
            return
        if not self._dedup("req", group_id, user_id):
            return

        try:
            if self._is_blacklisted(group_id, user_id):
                await self._answer_request(event, str(raw.get("flag") or ""), approve=False)
                await self._send(event, group_id, Plain(text=f"黑名单用户 {user_id} 的加群申请已自动拒绝"))
                return

            await self._send_notice(
                event,
                group_id,
                user_id,
                flag=str(raw.get("flag") or ""),
                comment=str(raw.get("comment") or "").strip(),
            )
        except Exception as exc:
            logger.error(f"[JoinReview] 处理加群申请失败: {exc}")

    async def _send_notice(self, event: AstrMessageEvent, group_id: str, user_id: str, flag: str = "", comment: str = "") -> None:
        nickname = await self._stranger_name(event, user_id) or user_id
        text = _fmt(self._on_text("request_template"), user_id=user_id, nickname=nickname, group_id=group_id, comment=comment)
        chain = []
        if self._on("box_on_request"):
            card = await self._render_card(event, group_id, user_id)
            if card is not None:
                chain.append(card)
        chain.append(Plain(text=text))

        sent = await self._send(event, group_id, *chain, want_result=True)
        message_id = str(sent.get("message_id") or "") if isinstance(sent, dict) else ""
        if not message_id:
            return
        self.pending[message_id] = {
            "stage": "request",
            "group_id": group_id,
            "user_id": user_id,
            "nickname": nickname,
            "flag": flag,
            "ts": time.time(),
        }
        self._prune_pending()
        self._save(self.pending_path, self.pending)

    # ------------------------------------------------------------------
    # 二、入群：非管理员时补发审核通知（开盒由 box 插件负责）
    # ------------------------------------------------------------------
    @event_message_type(EventMessageType.GROUP_MESSAGE, priority=999)
    async def on_group_increase(self, event: AstrMessageEvent):
        if not self._on("enable"):
            return
        raw = _as_dict(getattr(event.message_obj, "raw_message", None))
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_increase":
            return
        group_id = str(raw.get("group_id") or "").strip()
        user_id = str(raw.get("user_id") or "").strip()
        if not group_id or not user_id or user_id == str(event.get_self_id()):
            return
        if not self._group_enabled(group_id) or not self._dedup("in", group_id, user_id):
            return

        try:
            if self._is_blacklisted(group_id, user_id):
                if self._on("auto_kick_blacklist"):
                    await self._kick(event, group_id, user_id, reject_add=False)
                    await self._send(event, group_id, Plain(text=f"黑名单成员 {user_id} 已自动移出本群"))
                return
        except Exception as exc:
            logger.error(f"[JoinReview] 处理入群事件失败: {exc}")

    # ------------------------------------------------------------------
    # 四、审核指令
    # ------------------------------------------------------------------
    @event_message_type(EventMessageType.GROUP_MESSAGE, priority=800)
    async def on_review_command(self, event: AstrMessageEvent):
        if not self._on("enable"):
            return
        group_id = str(event.get_group_id() or "").strip()
        if not group_id or not self._group_enabled(group_id):
            return

        matched = COMMAND_RE.match(str(event.get_message_str() or "").strip())
        if not matched:
            return
        action = COMMANDS[matched.group(1)]

        reply: Reply | None = None
        at_qq = ""
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                reply = comp
            elif isinstance(comp, At) and not at_qq:
                at_qq = str(getattr(comp, "qq", "") or "").strip()

        target = self._resolve_target(reply, at_qq, matched.group(2))
        if not target.get("user_id"):
            await self._send(event, group_id, Plain(text="没找到要处理的人，请引用审核通知后再回复。"))
            event.stop_event()
            return

        if not await self._is_reviewer(event, group_id, event.get_sender_id()):
            await self._send(event, group_id, Plain(text="只有群主或管理员可以处理加群审核。"))
            event.stop_event()
            return

        user_id = str(target["user_id"])
        nickname = str(target.get("nickname") or "") or await self._stranger_name(event, user_id) or user_id
        stage = str(target.get("stage") or "request")

        if stage == "request":
            flag = str(target.get("flag") or "")
            approved = action == APPROVE
            await self._answer_request(event, flag, approve=approved, reject_forever=action == BLACKLIST)
            if action == BLACKLIST:
                self._add_blacklist(group_id, user_id)
                text = _fmt(self._on_text("blacklist_reply"), user_id=user_id, nickname=nickname, group_id=group_id)
            elif approved:
                text = _fmt(self._on_text("approve_reply"), user_id=user_id, nickname=nickname, group_id=group_id)
            else:
                text = _fmt(self._on_text("reject_reply"), user_id=user_id, nickname=nickname, group_id=group_id)
        else:
            if action == APPROVE:
                text = _fmt(self._on_text("approve_reply"), user_id=user_id, nickname=nickname, group_id=group_id)
            else:
                await self._kick(event, group_id, user_id, reject_add=action == BLACKLIST and self._on("reject_add_request"))
                if action == BLACKLIST:
                    self._add_blacklist(group_id, user_id)
                    text = _fmt(self._on_text("blacklist_reply"), user_id=user_id, nickname=nickname, group_id=group_id)
                else:
                    text = _fmt(self._on_text("reject_reply"), user_id=user_id, nickname=nickname, group_id=group_id)

        self._drop_pending(reply)
        await self._send(event, group_id, Plain(text=text))
        event.stop_event()

    def _resolve_target(self, reply: Reply | None, at_qq: str, tail: str | None) -> dict[str, Any]:
        if reply is not None:
            item = self.pending.get(str(reply.id))
            if item:
                return item
            found = re.search(r"(\d{5,12})", str(getattr(reply, "message_str", "") or ""))
            if found:
                return {"stage": "request", "user_id": found.group(1)}
        if at_qq:
            return {"stage": "request", "user_id": at_qq}
        if tail:
            found = re.search(r"(\d{5,12})", tail)
            if found:
                return {"stage": "request", "user_id": found.group(1)}
        return {}

    def _drop_pending(self, reply: Reply | None) -> None:
        if reply is not None and self.pending.pop(str(reply.id), None) is not None:
            self._save(self.pending_path, self.pending)

    def _prune_pending(self) -> None:
        now = time.time()
        for key in [k for k, v in self.pending.items() if now - float(v.get("ts", 0)) > PENDING_TTL]:
            self.pending.pop(key, None)

    # ------------------------------------------------------------------
    # 资料卡
    # ------------------------------------------------------------------
    async def _box_info(self, event: AstrMessageEvent, group_id: str, user_id: str, include_library: bool = False) -> BoxResult | None:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        try:
            return await self.box.get_box_info(
                bot,
                target_id=str(user_id),
                group_id=group_id or "0",
                include_library=include_library,
            )
        except Exception as exc:
            logger.debug(f"[JoinReview] 获取资料失败 {user_id}: {exc}")
            return None

    async def _render_card(self, event: AstrMessageEvent, group_id: str, user_id: str, include_library: bool = False) -> Any | None:
        result = await self._box_info(event, group_id, user_id, include_library=include_library)
        if result is None or result.is_fail():
            return None
        try:
            return Image.fromBytes(await self.box.render_box_image(result))
        except Exception as exc:
            logger.debug(f"[JoinReview] 渲染资料卡失败 {user_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # 权限 / 群范围 / 黑名单
    # ------------------------------------------------------------------
    async def _is_reviewer(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        if not user_id:
            return False
        if self._non_admin_allowed(group_id):
            return True
        if str(user_id) in self._list("extra_admins"):
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return await self._role(event, group_id, user_id) in ("owner", "admin")

    def _non_admin_allowed(self, group_id: str) -> bool:
        if not self._on("allow_non_admin"):
            return False
        if group_id in self._list("non_admin_blacklist"):
            return False
        white = self._list("non_admin_whitelist")
        return not white or group_id in white

    async def _role(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        info = await self._call(event, "get_group_member_info", group_id=int(group_id), user_id=int(user_id))
        return str(info.get("role") or "").strip().lower() if isinstance(info, dict) else ""

    def _group_enabled(self, group_id: str) -> bool:
        if group_id in self._list("blacklist_groups"):
            return False
        white = self._list("whitelist_groups")
        return not white or group_id in white

    def _is_blacklisted(self, group_id: str, user_id: str) -> bool:
        if user_id in self.blacklist.get("*", []):
            return True
        if self._on_text("blacklist_scope") == "global":
            return any(user_id in users for users in self.blacklist.values())
        return user_id in self.blacklist.get(group_id, [])

    def _add_blacklist(self, group_id: str, user_id: str) -> None:
        scope = "*" if self._on_text("blacklist_scope") == "global" else group_id
        users = self.blacklist.setdefault(scope, [])
        if user_id not in users:
            users.append(user_id)
        self._save(self.blacklist_path, self.blacklist)

    # ------------------------------------------------------------------
    # OneBot 调用
    # ------------------------------------------------------------------
    async def _call(self, event: AstrMessageEvent, action: str, **params: Any) -> Any:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        caller = getattr(getattr(bot, "api", None), "call_action", None) or getattr(bot, "call_action", None)
        if not callable(caller):
            return None
        try:
            return await caller(action, **params)
        except Exception as exc:
            logger.debug(f"[JoinReview] 调用 {action} 失败: {exc}")
            return None

    async def _answer_request(self, event: AstrMessageEvent, flag: str, approve: bool, reject_forever: bool = False) -> None:
        if not flag:
            return
        await self._call(
            event,
            "set_group_add_request",
            flag=flag,
            sub_type="add",
            approve=bool(approve),
            reason="" if approve else ("已被拉黑" if reject_forever else "管理员拒绝了你的加群申请"),
        )

    async def _kick(self, event: AstrMessageEvent, group_id: str, user_id: str, reject_add: bool) -> None:
        await self._call(
            event,
            "set_group_kick",
            group_id=int(group_id),
            user_id=int(user_id),
            reject_add_request=bool(reject_add),
        )

    async def _stranger_name(self, event: AstrMessageEvent, user_id: str) -> str:
        info = await self._call(event, "get_stranger_info", user_id=int(user_id), no_cache=True)
        return str(info.get("nickname") or "").strip() if isinstance(info, dict) else ""

    async def _send(self, event: AstrMessageEvent, group_id: str, *chain: Any, want_result: bool = False) -> Any:
        bot = getattr(event, "bot", None)
        parser = getattr(event, "_parse_onebot_json", None)
        if bot is not None and callable(parser):
            try:
                payload = await parser(MessageChain(chain=list(chain)))
                result = await bot.send_group_msg(group_id=int(group_id), message=payload)
                return result if want_result else True
            except Exception as exc:
                logger.debug(f"[JoinReview] send_group_msg 失败，改用 context 发送: {exc}")
        try:
            await self.context.send_message(event.unified_msg_origin, MessageChain(chain=list(chain)))
            return True
        except Exception as exc:
            logger.error(f"[JoinReview] 发送消息失败: {exc}")
            return None

    @staticmethod
    def _dedup(tag: str, group_id: str, user_id: str) -> bool:
        key = (tag, group_id, user_id)
        now = time.time()
        if _seen.get(key, 0) > now - DEDUP_TTL:
            return False
        _seen[key] = now
        return True

    # ------------------------------------------------------------------
    # 配置与存储
    # ------------------------------------------------------------------
    _defaults: dict[str, Any] = {}

    @classmethod
    def _schema_defaults(cls) -> dict[str, Any]:
        if not cls._defaults:
            try:
                raw = (Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8")
                cls._defaults = {k: v.get("default") for k, v in json.loads(raw).items()}
            except Exception:
                cls._defaults = {}
        return cls._defaults

    def _raw(self, key: str) -> Any:
        try:
            if key in self.config:
                return self.config[key]
        except Exception:
            pass
        return self._schema_defaults().get(key)

    def _on(self, key: str) -> bool:
        value = self._raw(key)
        return True if value is None else bool(value)

    def _on_text(self, key: str) -> str:
        value = self._raw(key)
        return str(value) if value is not None else ""

    def _on_num(self, key: str) -> int:
        try:
            return int(self._raw(key) or 0)
        except (TypeError, ValueError):
            return 0

    def _list(self, key: str) -> list[str]:
        value = self._raw(key)
        if isinstance(value, str):
            value = [v for v in re.split(r"[\s,;，；]+", value) if v]
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[JoinReview] 读取 {path.name} 失败: {exc}")
        return default

    @staticmethod
    def _save(path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error(f"[JoinReview] 写入 {path.name} 失败: {exc}")
