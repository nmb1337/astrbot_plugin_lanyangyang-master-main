import asyncio
import json
import hashlib
import random
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except Exception:  # pragma: no cover - handled at runtime inside _render_card
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None


PLUGIN_NAME = "astrbot_plugin_lanyangyang"


@register(
    "astrbot_plugin_lanyangyang",
    "Codex",
    "懒羊羊主题基础群管：发言统计、邀请排行、禁言、撤回、批量撤回、踢黑、白名单、点歌、图片回复与语音",
    "1.1.0",
)
class LanYangYangGroupManager(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.base_dir = Path(__file__).parent
        self.data_dir = self.base_dir / "data"
        self.cache_dir = self.base_dir / "cache"
        self.voice_dir = self.base_dir / "voices"
        self.data_file = self.data_dir / "lanyangyang_stats.json"
        self.data_dir.mkdir(exist_ok=True)
        self.cache_dir.mkdir(exist_ok=True)
        self.voice_dir.mkdir(exist_ok=True)
        self.stats = self._load_stats()

    async def initialize(self):
        logger.info("懒羊羊群管插件已加载。发送“菜单”可免唤醒查看命令。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_every_message(self, event: AstrMessageEvent):
        await self._record_message(event)
        await self._record_invite_from_raw(event)

        text = (event.message_str or "").strip()
        if self._should_pass_to_other_plugin(text):
            return

        direct_result = await self._direct_result(event, text)
        if direct_result:
            yield direct_result
            event.stop_event()
            return

        moderation_result = await self._moderation_result(event, text)
        if moderation_result:
            yield moderation_result
            event.stop_event()
            return

        if self._should_lazy_voice(event, text):
            result = await self._lazy_voice_or_card(event)
            yield result
            event.stop_event()

    @filter.command("菜单", alias={"帮助", "懒羊羊菜单"})
    async def menu(self, event: AstrMessageEvent):
        """显示懒羊羊群管菜单。"""
        yield await self._image_result(event, "懒羊羊菜单", self._menu_lines())
        event.stop_event()

    @filter.command("今日老公", alias={"抽老公"})
    async def today_husband(self, event: AstrMessageEvent):
        """抽取今日老公。"""
        yield await self._today_partner_result(event, "今日老公")

    @filter.command("今日老婆", alias={"抽老婆"})
    async def today_wife(self, event: AstrMessageEvent):
        """抽取今日老婆。"""
        yield await self._today_partner_result(event, "今日老婆")

    @filter.command("今日小三", alias={"抽小三"})
    async def today_affair(self, event: AstrMessageEvent):
        """抽取今日小三。"""
        yield await self._today_partner_result(event, "今日小三")

    @filter.command("发言统计", alias={"统计", "水群排行"})
    async def speech_rank(self, event: AstrMessageEvent):
        """查看本群发言排行。"""
        group_id = self._group_id(event)
        if not group_id:
            yield await self._image_result(event, "发言统计", ["这个功能要在群聊里用。"])
            return

        members = self.stats["groups"].get(group_id, {}).get("members", {})
        ranking = sorted(
            members.items(), key=lambda item: item[1].get("count", 0), reverse=True
        )[:10]
        if not ranking:
            lines = ["还没有统计到发言。"]
        else:
            lines = [
                f"{idx}. {info.get('name') or uid}: {info.get('count', 0)} 条 / {info.get('chars', 0)} 字"
                for idx, (uid, info) in enumerate(ranking, 1)
            ]
        yield await self._image_result(event, "本群发言排行", lines)

    @filter.command("我的统计", alias={"我水了多少"})
    async def my_speech(self, event: AstrMessageEvent):
        """查看自己的发言统计。"""
        group_id = self._group_id(event)
        user_id = str(event.get_sender_id())
        info = self.stats["groups"].get(group_id, {}).get("members", {}).get(user_id)
        if not info:
            lines = ["还没有统计到你的发言。"]
        else:
            last = self._format_time(info.get("last_active"))
            lines = [
                f"昵称：{info.get('name') or event.get_sender_name()}",
                f"发言：{info.get('count', 0)} 条",
                f"字数：{info.get('chars', 0)} 字",
                f"最近：{last}",
            ]
        yield await self._image_result(event, "我的发言统计", lines)

    @filter.command("邀请排行", alias={"邀请榜", "邀请统计"})
    async def invite_rank(self, event: AstrMessageEvent):
        """查看本群邀请排行。"""
        group_id = self._group_id(event)
        rows = self.stats["invites"].get(group_id, {})
        ranking = sorted(rows.items(), key=lambda item: item[1].get("count", 0), reverse=True)[:10]
        if not ranking:
            lines = [
                "暂时没有邀请记录。",
                "如果协议端没有上报入群邀请人，可以用：记邀请 @邀请人",
            ]
        else:
            lines = [
                f"{idx}. {info.get('name') or uid}: 邀请 {info.get('count', 0)} 人"
                for idx, (uid, info) in enumerate(ranking, 1)
            ]
        yield await self._image_result(event, "邀请排行", lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("记邀请", alias={"登记邀请"})
    async def record_invite(self, event: AstrMessageEvent):
        """手动登记一次邀请。"""
        group_id = self._group_id(event)
        inviter = self._extract_target_user(event)
        if not group_id or not inviter:
            yield await self._image_result(event, "登记邀请", ["用法：记邀请 @邀请人"])
            return
        uid, name = inviter
        self._add_invite(group_id, uid, name)
        self._save_stats()
        yield await self._image_result(event, "登记邀请", [f"已给 {name or uid} 记 1 次邀请。"])

    @filter.command("语音", alias={"懒羊羊语音", "发语音"})
    async def voice(self, event: AstrMessageEvent):
        """主动发送 voices 目录里的语音。"""
        yield await self._voice_result(event)

    @filter.command("点歌", alias={"音乐", "来首歌"})
    async def music(self, event: AstrMessageEvent):
        """发送 QQ/网易云音乐卡片。"""
        yield await self._music_result(event)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁言", alias={"闭嘴"})
    async def mute(self, event: AstrMessageEvent):
        """禁言群成员。"""
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        duration = self._extract_duration(event, default_seconds=600)
        if not group_id or not target:
            yield await self._image_result(event, "禁言", ["用法：禁言 @成员 10m"])
            return
        valid, validation_msg = await self._ensure_group_member(event, group_id, target)
        if not valid:
            yield await self._image_result(event, "禁言结果", [validation_msg])
            return
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target[0]),
            duration=duration,
        )
        lines = [f"{target[1] or target[0]} 禁言 {self._human_duration(duration)}", msg]
        yield await self._image_result(event, "禁言结果", lines if ok else [msg])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("解禁", alias={"解除禁言"})
    async def unmute(self, event: AstrMessageEvent):
        """解除群成员禁言。"""
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        if not group_id or not target:
            yield await self._image_result(event, "解禁", ["用法：解禁 @成员"])
            return
        valid, validation_msg = await self._ensure_group_member(event, group_id, target)
        if not valid:
            yield await self._image_result(event, "解禁结果", [validation_msg])
            return
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target[0]),
            duration=0,
        )
        yield await self._image_result(
            event, "解禁结果", [f"{target[1] or target[0]} 已解禁。", msg] if ok else [msg]
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回", alias={"删"})
    async def recall(self, event: AstrMessageEvent):
        """撤回一条消息，支持回复消息后发送“撤回”。"""
        message_id = self._extract_reply_message_id(event) or self._extract_first_number(event)
        if not message_id:
            yield await self._image_result(event, "撤回", ["请回复要撤回的消息，或发送：撤回 消息ID"])
            return
        ok, msg = await self._onebot_call(event, "delete_msg", message_id=int(message_id))
        yield await self._image_result(event, "撤回结果", [msg if ok else f"撤回失败：{msg}"])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量撤回", alias={"批撤"})
    async def batch_recall(self, event: AstrMessageEvent):
        """批量撤回最近消息。"""
        group_id = self._group_id(event)
        if not group_id:
            yield await self._image_result(event, "批量撤回", ["这个功能要在群聊里用。"])
            return
        count = max(1, min(self._extract_count(event, default=5), 50))
        target = self._extract_target_user(event)
        ids = self._recent_message_ids(group_id, count, target[0] if target else None, event)
        success = 0
        errors = []
        for msg_id in ids:
            ok, msg = await self._onebot_call(event, "delete_msg", message_id=int(msg_id))
            success += 1 if ok else 0
            if not ok:
                errors.append(msg)
        lines = [f"目标：最近 {count} 条", f"成功撤回：{success} 条"]
        if errors:
            lines.append(f"失败：{errors[0]}")
        yield await self._image_result(event, "批量撤回结果", lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("踢出群", alias={"踢出", "踢", "踢了"})
    async def kick(self, event: AstrMessageEvent):
        """踢出群成员，不拉黑。"""
        yield await self._kick_impl(event, reject=False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("踢黑", alias={"拉黑踢出", "拉黑"})
    async def kick_black(self, event: AstrMessageEvent):
        """踢出并拒绝再次加群。"""
        yield await self._kick_impl(event, reject=True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁我")
    async def mute_me(self, event: AstrMessageEvent):
        """禁言自己。"""
        group_id = self._group_id(event)
        duration = self._extract_duration(event, default_seconds=600)
        if not group_id:
            yield await self._image_result(event, "禁我", ["这个功能要在群聊里用。"])
            return
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(event.get_sender_id()),
            duration=duration,
        )
        yield await self._image_result(event, "禁我结果", [f"禁言自己 {self._human_duration(duration)}", msg] if ok else [msg])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启全禁", alias={"全禁"})
    async def whole_ban_on(self, event: AstrMessageEvent):
        yield await self._whole_ban_result(event, True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭全禁", alias={"解除全禁"})
    async def whole_ban_off(self, event: AstrMessageEvent):
        yield await self._whole_ban_result(event, False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改名", alias={"设置群名片"})
    async def set_member_card(self, event: AstrMessageEvent):
        yield await self._set_card_result(event, self._extract_target_user(event), self._clean_command_text(event))

    @filter.command("改我")
    async def set_my_card(self, event: AstrMessageEvent):
        yield await self._set_card_result(event, (str(event.get_sender_id()), event.get_sender_name()), self._clean_command_text(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("头衔", alias={"改头衔"})
    async def set_special_title(self, event: AstrMessageEvent):
        yield await self._set_title_result(event, self._extract_target_user(event), self._clean_command_text(event))

    @filter.command("申请头衔")
    async def apply_special_title(self, event: AstrMessageEvent):
        yield await self._set_title_result(event, (str(event.get_sender_id()), event.get_sender_name()), self._clean_command_text(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("上管")
    async def admin_on(self, event: AstrMessageEvent):
        yield await self._set_admin_result(event, True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("下管")
    async def admin_off(self, event: AstrMessageEvent):
        yield await self._set_admin_result(event, False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("白名单", alias={"查白", "查看白名单"})
    async def whitelist(self, event: AstrMessageEvent):
        """查看白名单，或发送 白名单 @成员 添加。"""
        yield await self._whitelist_result(event, "auto")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拉白", alias={"加白", "加入白名单"})
    async def whitelist_add(self, event: AstrMessageEvent):
        """加入自动处罚白名单。"""
        yield await self._whitelist_result(event, "add")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删白", alias={"移白", "取消白名单"})
    async def whitelist_remove(self, event: AstrMessageEvent):
        """移出自动处罚白名单。"""
        yield await self._whitelist_result(event, "remove")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设精", alias={"设置精华"})
    async def set_essence(self, event: AstrMessageEvent):
        yield await self._essence_result(event, "set_essence_msg", "设精")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("移精", alias={"移除精华"})
    async def delete_essence(self, event: AstrMessageEvent):
        yield await self._essence_result(event, "delete_essence_msg", "移精")

    @filter.command("查看群精华")
    async def list_essence(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            yield await self._image_result(event, "查看群精华", ["这个功能要在群聊里用。"])
            return
        ok, data = await self._onebot_call_raw(event, "get_essence_msg_list", group_id=int(group_id))
        if not ok:
            yield await self._image_result(event, "查看群精华", [str(data)])
            return
        rows = data if isinstance(data, list) else []
        lines = [f"共 {len(rows)} 条群精华。"] + [
            f"{idx}. {row.get('sender_nick') or row.get('sender_id')}: {row.get('message_id')}"
            for idx, row in enumerate(rows[:8], 1)
            if isinstance(row, dict)
        ]
        yield await self._image_result(event, "查看群精华", lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置群名")
    async def set_group_name(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        name = self._command_args(event).strip()
        if not group_id or not name:
            yield await self._image_result(event, "设置群名", ["用法：设置群名 新群名"])
            return
        ok, msg = await self._onebot_call(event, "set_group_name", group_id=int(group_id), group_name=name)
        yield await self._image_result(event, "设置群名", [f"新群名：{name}", msg] if ok else [msg])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置群头像")
    async def set_group_portrait(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        image = self._extract_image_url(event)
        if not group_id or not image:
            yield await self._image_result(event, "设置群头像", ["用法：引用/发送图片并输入 设置群头像"])
            return
        ok, msg = await self._onebot_call(event, "set_group_portrait", group_id=int(group_id), file=image, cache=0)
        yield await self._image_result(event, "设置群头像", [msg])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发布群公告")
    async def send_group_notice(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        content = self._command_args(event).strip()
        image = self._extract_image_url(event)
        if not group_id or not content:
            yield await self._image_result(event, "发布群公告", ["用法：发布群公告 公告内容，可同时带图"])
            return
        payload = {"group_id": int(group_id), "content": content}
        if image:
            payload["image"] = image
        ok, msg = await self._onebot_call(event, "_send_group_notice", **payload)
        yield await self._image_result(event, "发布群公告", [content, msg] if ok else [msg])

    @filter.command("查看群公告")
    async def get_group_notice(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            yield await self._image_result(event, "查看群公告", ["这个功能要在群聊里用。"])
            return
        ok, data = await self._onebot_call_raw(event, "_get_group_notice", group_id=int(group_id))
        if not ok:
            yield await self._image_result(event, "查看群公告", [str(data)])
            return
        rows = data if isinstance(data, list) else []
        lines = [f"共 {len(rows)} 条公告。"] + [
            f"{idx}. {(row.get('message') or row.get('content') or '')[:22]}"
            for idx, row in enumerate(rows[:8], 1)
            if isinstance(row, dict)
        ]
        yield await self._image_result(event, "查看群公告", lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁词禁言")
    async def banned_word_mute_seconds(self, event: AstrMessageEvent):
        yield await self._set_group_setting_result(event, "禁词禁言", "banned_word_mute_seconds", self._extract_duration(event, 600))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置禁词")
    async def set_banned_words(self, event: AstrMessageEvent):
        text = self._command_args(event).strip()
        words = [item.strip() for item in re.split(r"[，,\s]+", text) if item.strip()]
        settings = self._group_settings(event)
        if text.startswith("+"):
            settings["banned_words"] = sorted(set(settings.get("banned_words", [])) | set(word.lstrip("+") for word in words))
        elif text.startswith("-"):
            remove = {word.lstrip("-") for word in words}
            settings["banned_words"] = [word for word in settings.get("banned_words", []) if word not in remove]
        else:
            settings["banned_words"] = words
        self._save_stats()
        yield await self._image_result(event, "设置禁词", [f"当前禁词：{'、'.join(settings.get('banned_words', [])) or '空'}"])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("内置禁词")
    async def builtin_banned_words(self, event: AstrMessageEvent):
        value = "关" not in self._clean_command_text(event)
        yield await self._set_group_setting_result(event, "内置禁词", "builtin_banned_words", value)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("刷屏禁言")
    async def spam_mute_seconds(self, event: AstrMessageEvent):
        yield await self._set_group_setting_result(event, "刷屏禁言", "spam_mute_seconds", self._extract_duration(event, 600))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("投票禁言")
    async def vote_mute(self, event: AstrMessageEvent):
        yield await self._start_vote_mute(event)

    @filter.command("赞同禁言")
    async def vote_agree(self, event: AstrMessageEvent):
        yield await self._vote_mute_result(event, agree=True)

    @filter.command("反对禁言")
    async def vote_disagree(self, event: AstrMessageEvent):
        yield await self._vote_mute_result(event, agree=False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启宵禁")
    async def night_curfew_on(self, event: AstrMessageEvent):
        yield await self._set_group_setting_result(event, "开启宵禁", "night_curfew", True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭宵禁")
    async def night_curfew_off(self, event: AstrMessageEvent):
        yield await self._set_group_setting_result(event, "关闭宵禁", "night_curfew", False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("进群审核")
    async def join_approval(self, event: AstrMessageEvent):
        value = "关" not in self._clean_command_text(event)
        yield await self._set_group_setting_result(event, "进群审核", "join_approval", value)

    @filter.on_decorating_result()
    async def decorate_text_reply(self, event: AstrMessageEvent):
        if not self._bool_config("convert_all_text_reply", True):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain or not self._is_text_only_chain(chain):
            return
        text = "\n".join(self._component_text(item) for item in chain).strip()
        if not text:
            return
        path = await self._render_card(event, "懒羊羊回复", text)
        result.chain = [Comp.Image.fromFileSystem(str(path))]

    async def _kick_impl(self, event: AstrMessageEvent, reject: bool):
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        if not group_id or not target:
            return await self._image_result(event, "踢出群", ["用法：踢出群 @成员 或 踢黑 @成员"])
        valid, validation_msg = await self._ensure_group_member(event, group_id, target)
        if not valid:
            return await self._image_result(event, "踢出结果", [validation_msg])
        role_ok, role_msg = await self._ensure_role_action_allowed(event, group_id, target, "kick")
        if not role_ok:
            action = "踢黑" if reject else "踢出"
            return await self._image_result(event, f"{action}结果", [role_msg])
        ok, msg = await self._onebot_call(
            event,
            "set_group_kick",
            group_id=int(group_id),
            user_id=int(target[0]),
            reject_add_request=reject,
        )
        action = "踢黑" if reject else "踢出"
        lines = [f"{action}：{target[1] or target[0]}", msg]
        return await self._image_result(event, f"{action}结果", lines if ok else [msg])

    async def _whole_ban_result(self, event: AstrMessageEvent, enable: bool):
        group_id = self._group_id(event)
        if not group_id:
            return await self._image_result(event, "全体禁言", ["这个功能要在群聊里用。"])
        ok, msg = await self._onebot_call(event, "set_group_whole_ban", group_id=int(group_id), enable=enable)
        title = "开启全禁" if enable else "关闭全禁"
        return await self._image_result(event, title, [msg])

    async def _set_card_result(self, event: AstrMessageEvent, target: tuple[str, str] | None, card: str):
        group_id = self._group_id(event)
        if not group_id or not target or not card:
            return await self._image_result(event, "改名", ["用法：改名 新名 @群友，或 改我 新名"])
        ok, msg = await self._onebot_call(
            event,
            "set_group_card",
            group_id=int(group_id),
            user_id=int(target[0]),
            card=card,
        )
        return await self._image_result(event, "改名结果", [f"{target[1] or target[0]} -> {card}", msg] if ok else [msg])

    async def _set_title_result(self, event: AstrMessageEvent, target: tuple[str, str] | None, title: str):
        group_id = self._group_id(event)
        if not group_id or not target or not title:
            return await self._image_result(event, "头衔", ["用法：头衔 新头衔 @群友，或 申请头衔 新头衔"])
        ok, msg = await self._onebot_call(
            event,
            "set_group_special_title",
            group_id=int(group_id),
            user_id=int(target[0]),
            special_title=title,
            duration=-1,
        )
        return await self._image_result(event, "头衔结果", [f"{target[1] or target[0]}：{title}", msg] if ok else [msg])

    async def _set_admin_result(self, event: AstrMessageEvent, enable: bool):
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        if not group_id or not target:
            return await self._image_result(event, "群管设置", ["用法：上管 @群友，或 下管 @群友"])
        valid, validation_msg = await self._ensure_group_member(event, group_id, target)
        if not valid:
            title = "上管" if enable else "下管"
            return await self._image_result(event, f"{title}结果", [validation_msg])
        role_ok, role_msg = await self._ensure_role_action_allowed(event, group_id, target, "admin")
        if not role_ok:
            title = "上管" if enable else "下管"
            return await self._image_result(event, f"{title}结果", [role_msg])
        ok, msg = await self._onebot_call(
            event,
            "set_group_admin",
            group_id=int(group_id),
            user_id=int(target[0]),
            enable=enable,
        )
        title = "上管" if enable else "下管"
        return await self._image_result(event, f"{title}结果", [f"{target[1] or target[0]}", msg] if ok else [msg])

    async def _whitelist_result(self, event: AstrMessageEvent, mode: str):
        group_id = self._group_id(event)
        if not group_id:
            return await self._image_result(event, "白名单", ["这个功能要在群聊里用。"])

        settings = self._group_settings(event)
        whitelist = self._whitelist_map(settings)
        target = self._extract_target_user(event)
        text = self._clean_command_text(event)
        if mode == "auto":
            if any(key in text for key in ("删", "移", "取消", "-")):
                mode = "remove"
            elif target:
                mode = "add"
            else:
                mode = "list"

        if mode == "list":
            if not whitelist:
                lines = ["当前白名单为空。", "用法：拉白 @群友 / 删白 @群友"]
            else:
                lines = [
                    f"{idx}. {name or uid} ({uid})"
                    for idx, (uid, name) in enumerate(whitelist.items(), 1)
                ][:20]
            return await self._image_result(event, "白名单", lines)

        if not target:
            return await self._image_result(event, "白名单", ["用法：拉白 @群友 / 删白 @群友 / 白名单"])

        uid, name = target
        if mode == "remove":
            existed = whitelist.pop(uid, None)
            self._save_stats()
            lines = [f"已移出：{existed or name or uid}" if existed else f"{name or uid} 不在白名单。"]
            return await self._image_result(event, "删白", lines)

        whitelist[uid] = name or uid
        self._save_stats()
        return await self._image_result(event, "拉白", [f"已加入白名单：{name or uid}", "禁词、刷屏、宵禁自动处罚会跳过此人。"])

    async def _essence_result(self, event: AstrMessageEvent, action: str, title: str):
        message_id = self._extract_reply_message_id(event) or self._extract_first_number(event)
        if not message_id:
            return await self._image_result(event, title, [f"请回复消息后发送：{title}"])
        ok, msg = await self._onebot_call(event, action, message_id=int(message_id))
        return await self._image_result(event, f"{title}结果", [msg])

    async def _set_group_setting_result(self, event: AstrMessageEvent, title: str, key: str, value: Any):
        settings = self._group_settings(event)
        settings[key] = value
        self._save_stats()
        if isinstance(value, bool):
            value_text = "开启" if value else "关闭"
        elif isinstance(value, int):
            value_text = self._human_duration(value)
        else:
            value_text = str(value)
        return await self._image_result(event, title, [f"已设置：{value_text}"])

    async def _start_vote_mute(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        duration = self._extract_duration(event, 600)
        if not group_id or not target:
            return await self._image_result(event, "投票禁言", ["用法：投票禁言 <秒数> @群友"])
        votes = self.stats.setdefault("votes", {})
        votes[group_id] = {
            "target": target[0],
            "name": target[1],
            "duration": duration,
            "agree": [],
            "disagree": [],
            "created": int(time.time()),
        }
        self._save_stats()
        return await self._image_result(
            event,
            "投票禁言",
            [f"目标：{target[1] or target[0]}", f"时长：{self._human_duration(duration)}", "发送 赞同禁言 / 反对禁言 参与投票"],
        )

    async def _vote_mute_result(self, event: AstrMessageEvent, agree: bool):
        group_id = self._group_id(event)
        vote = self.stats.setdefault("votes", {}).get(group_id)
        if not group_id or not vote:
            return await self._image_result(event, "投票禁言", ["当前没有进行中的禁言投票。"])
        user_id = str(event.get_sender_id())
        vote["agree"] = [uid for uid in vote.get("agree", []) if uid != user_id]
        vote["disagree"] = [uid for uid in vote.get("disagree", []) if uid != user_id]
        vote["agree" if agree else "disagree"].append(user_id)
        agree_count = len(set(vote.get("agree", [])))
        disagree_count = len(set(vote.get("disagree", [])))
        lines = [f"赞同：{agree_count}", f"反对：{disagree_count}"]
        if agree_count >= 3 and agree_count > disagree_count:
            ok, msg = await self._onebot_call(
                event,
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(vote["target"]),
                duration=int(vote.get("duration", 600)),
            )
            self.stats["votes"].pop(group_id, None)
            lines.append(msg if ok else f"执行失败：{msg}")
        self._save_stats()
        return await self._image_result(event, "投票禁言", lines)

    async def _voice_result(self, event: AstrMessageEvent):
        query = self._clean_command_text(event)
        voices = self._voice_files()
        if query:
            matched = [path for path in voices if query.lower() in path.stem.lower()]
            if matched:
                voices = matched
        if not voices:
            return await self._image_result(
                event,
                "语音",
                ["voices 目录还没有可发送的语音。", "支持 wav / mp3 / amr / silk，发送：语音 或 语音 文件名关键词"],
            )
        path = str(random.choice(voices))
        if hasattr(Comp.Record, "fromFileSystem"):
            record = Comp.Record.fromFileSystem(path)
        else:
            record = Comp.Record(file=path, url=path)
        return event.chain_result([record])

    async def _music_result(self, event: AstrMessageEvent):
        raw = self._command_args(event).strip()
        source, song_id = self._parse_music_request(raw)
        song_name = ""
        artist = ""
        if raw and not song_id:
            query = self._extract_music_query(raw, source)
            if query:
                found = await self._search_music_song(source, query)
                if found:
                    source, song_id, song_name, artist = found
        if not song_id:
            return await self._image_result(
                event,
                "点歌",
                [
                    "用法：点歌 海阔天空，点歌 qq 海阔天空，或 点歌 网易 海阔天空",
                    "也支持：点歌 qq 123456，点歌 网易 123456。",
                    "没有搜索到歌曲时，请换关键词或直接发送音乐 ID。",
                ],
            )

        ok, music_status = await self._try_send_music_card(event, source, song_id, song_name, artist)
        display_name = f"{song_name} - {artist}".strip(" -") if song_name or artist else ""
        return await self._image_result(
            event,
            "点歌",
            ([f"歌曲：{display_name}"] if display_name else []) + [
                f"来源：{source}",
                f"歌曲 ID：{song_id}",
                music_status if ok else f"发送失败：{music_status}",
            ],
        )

    async def _try_send_music_card(
        self,
        event: AstrMessageEvent,
        source: str,
        song_id: str,
        song_name: str = "",
        artist: str = "",
    ) -> tuple[bool, str]:
        if source == "163":
            segment = {"type": "music", "data": {"type": "163", "id": str(song_id)}}
            ok, msg = await self._send_onebot_music_segment(event, segment)
            if ok:
                return True, "网易云音乐卡片已发送。"
            logger.warning("网易云音乐 ID 卡片发送失败，尝试 custom 卡片：%s", msg)

        card = self._custom_music_card(source, song_id, song_name, artist)
        ok, msg = await self._send_onebot_music_segment(event, card)
        if ok:
            return True, "音乐卡片已发送。"
        return False, msg

    async def _send_onebot_music_segment(self, event: AstrMessageEvent, segment: dict):
        group_id = self._group_id(event)
        if group_id:
            return await self._onebot_call(
                event,
                "send_group_msg",
                group_id=int(group_id),
                message=[segment],
            )
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return False, "无法获取接收者。"
        return await self._onebot_call(
            event,
            "send_private_msg",
            user_id=int(user_id),
            message=[segment],
        )

    def _custom_music_card(self, source: str, song_id: str, song_name: str = "", artist: str = "") -> dict:
        if source == "163":
            url = f"https://music.163.com/song?id={song_id}"
            audio = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"
            title = song_name or f"网易云音乐 {song_id}"
            content = artist or "点击打开网易云音乐"
            image = "https://s1.music.126.net/style/favicon.ico"
        else:
            url = f"https://y.qq.com/n/ryqq/songDetail/{song_id}"
            audio = url
            title = song_name or f"QQ音乐 {song_id}"
            content = artist or "点击打开 QQ 音乐"
            image = "https://y.qq.com/favicon.ico"
        return {
            "type": "music",
            "data": {
                "type": "custom",
                "url": url,
                "audio": audio,
                "title": title,
                "content": content,
                "image": image,
            },
        }

    async def _image_result(self, event: AstrMessageEvent, title: str, lines: list[str] | str):
        try:
            path = await self._render_card(event, title, lines)
            return event.chain_result([Comp.Image.fromFileSystem(str(path))])
        except Exception as exc:
            logger.exception("生成懒羊羊图片失败")
            text = "\n".join(lines) if isinstance(lines, list) else str(lines)
            return event.plain_result(f"{title}\n{text}\n\n图片生成失败：{exc}")

    async def _direct_result(self, event: AstrMessageEvent, text: str):
        command = text.strip().lstrip("/")
        command_name = self._matched_command_name(command)
        if command in self._list_config("direct_menu_keywords", ["菜单", "帮助", "懒羊羊菜单"]):
            return await self._image_result(event, "懒羊羊菜单", self._menu_lines())
        if command in {"发言统计", "统计", "水群排行"}:
            return await self._speech_rank_result(event)
        if command in {"我的统计", "我水了多少"}:
            return await self._my_speech_result(event)
        if command in {"邀请排行", "邀请榜", "邀请统计"}:
            return await self._invite_rank_result(event)
        if command in {"今日老公", "抽老公"}:
            return await self._today_partner_result(event, "今日老公")
        if command in {"今日老婆", "抽老婆"}:
            return await self._today_partner_result(event, "今日老婆")
        if command in {"今日小三", "抽小三"}:
            return await self._today_partner_result(event, "今日小三")
        if not command_name:
            return None

        handlers = {
            "菜单": lambda: self._image_result(event, "懒羊羊菜单", self._menu_lines()),
            "帮助": lambda: self._image_result(event, "懒羊羊菜单", self._menu_lines()),
            "懒羊羊菜单": lambda: self._image_result(event, "懒羊羊菜单", self._menu_lines()),
            "发言统计": lambda: self._speech_rank_result(event),
            "统计": lambda: self._speech_rank_result(event),
            "水群排行": lambda: self._speech_rank_result(event),
            "我的统计": lambda: self._my_speech_result(event),
            "我水了多少": lambda: self._my_speech_result(event),
            "邀请排行": lambda: self._invite_rank_result(event),
            "邀请榜": lambda: self._invite_rank_result(event),
            "邀请统计": lambda: self._invite_rank_result(event),
            "今日老公": lambda: self._today_partner_result(event, "今日老公"),
            "抽老公": lambda: self._today_partner_result(event, "今日老公"),
            "今日老婆": lambda: self._today_partner_result(event, "今日老婆"),
            "抽老婆": lambda: self._today_partner_result(event, "今日老婆"),
            "今日小三": lambda: self._today_partner_result(event, "今日小三"),
            "抽小三": lambda: self._today_partner_result(event, "今日小三"),
            "语音": lambda: self._voice_result(event),
            "懒羊羊语音": lambda: self._voice_result(event),
            "发语音": lambda: self._voice_result(event),
            "点歌": lambda: self._music_result(event),
            "音乐": lambda: self._music_result(event),
            "来首歌": lambda: self._music_result(event),
            "禁言": lambda: self._mute_result(event),
            "闭嘴": lambda: self._mute_result(event),
            "安静": lambda: self._whole_ban_result(event, True),
            "解禁": lambda: self._unmute_result(event),
            "解除禁言": lambda: self._unmute_result(event),
            "开启全禁": lambda: self._whole_ban_result(event, True),
            "全禁": lambda: self._whole_ban_result(event, True),
            "关闭全禁": lambda: self._whole_ban_result(event, False),
            "解除全禁": lambda: self._whole_ban_result(event, False),
            "撤回": lambda: self._recall_result(event),
            "删": lambda: self._recall_result(event),
            "批量撤回": lambda: self._batch_recall_result(event),
            "批撤": lambda: self._batch_recall_result(event),
            "踢出群": lambda: self._kick_impl(event, reject=False),
            "踢出": lambda: self._kick_impl(event, reject=False),
            "踢了": lambda: self._kick_impl(event, reject=False),
            "踢": lambda: self._kick_impl(event, reject=False),
            "踢黑": lambda: self._kick_impl(event, reject=True),
            "拉黑踢出": lambda: self._kick_impl(event, reject=True),
            "拉黑": lambda: self._kick_impl(event, reject=True),
            "改名": lambda: self._set_card_result(event, self._extract_target_user(event), self._clean_command_text(event)),
            "设置群名片": lambda: self._set_card_result(event, self._extract_target_user(event), self._clean_command_text(event)),
            "改我": lambda: self._set_card_result(event, (str(event.get_sender_id()), event.get_sender_name()), self._clean_command_text(event)),
            "头衔": lambda: self._set_title_result(event, self._extract_target_user(event), self._clean_command_text(event)),
            "改头衔": lambda: self._set_title_result(event, self._extract_target_user(event), self._clean_command_text(event)),
            "申请头衔": lambda: self._set_title_result(event, (str(event.get_sender_id()), event.get_sender_name()), self._clean_command_text(event)),
            "上管": lambda: self._set_admin_result(event, True),
            "授权": lambda: self._set_admin_result(event, True),
            "发权": lambda: self._set_admin_result(event, True),
            "下管": lambda: self._set_admin_result(event, False),
            "白名单": lambda: self._whitelist_result(event, "auto"),
            "查白": lambda: self._whitelist_result(event, "list"),
            "查看白名单": lambda: self._whitelist_result(event, "list"),
            "拉白": lambda: self._whitelist_result(event, "add"),
            "加白": lambda: self._whitelist_result(event, "add"),
            "加入白名单": lambda: self._whitelist_result(event, "add"),
            "删白": lambda: self._whitelist_result(event, "remove"),
            "移白": lambda: self._whitelist_result(event, "remove"),
            "取消白名单": lambda: self._whitelist_result(event, "remove"),
            "设精": lambda: self._essence_result(event, "set_essence_msg", "设精"),
            "设置精华": lambda: self._essence_result(event, "set_essence_msg", "设精"),
            "移精": lambda: self._essence_result(event, "delete_essence_msg", "移精"),
            "移除精华": lambda: self._essence_result(event, "delete_essence_msg", "移精"),
            "查看群精华": lambda: self._list_essence_result(event),
            "设置群头像": lambda: self._set_group_portrait_result(event),
            "设置群名": lambda: self._set_group_name_result(event),
            "发布群公告": lambda: self._send_group_notice_result(event),
            "查看群公告": lambda: self._get_group_notice_result(event),
            "禁词禁言": lambda: self._set_group_setting_result(event, "禁词禁言", "banned_word_mute_seconds", self._extract_duration(event, 600)),
            "设置禁词": lambda: self._set_banned_words_result(event),
            "内置禁词": lambda: self._builtin_banned_words_result(event),
            "刷屏禁言": lambda: self._set_group_setting_result(event, "刷屏禁言", "spam_mute_seconds", self._extract_duration(event, 0)),
            "投票禁言": lambda: self._start_vote_mute(event),
            "赞同禁言": lambda: self._vote_mute_result(event, True),
            "反对禁言": lambda: self._vote_mute_result(event, False),
            "开启宵禁": lambda: self._set_group_setting_result(event, "宵禁", "night_curfew", True),
            "关闭宵禁": lambda: self._set_group_setting_result(event, "宵禁", "night_curfew", False),
            "进群审核": lambda: self._set_group_setting_result(event, "进群审核", "join_approval", "开启" in command or "开" in command),
        }
        handler = handlers.get(command_name)
        if handler:
            return await handler()
        return None

    async def _mute_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        duration = self._extract_duration(event, default_seconds=600)
        if not group_id or not target:
            return await self._image_result(event, "禁言", ["用法：禁言 @成员 10m"])
        valid, validation_msg = await self._ensure_group_member(event, group_id, target)
        if not valid:
            return await self._image_result(event, "禁言结果", [validation_msg])
        role_ok, role_msg = await self._ensure_role_action_allowed(event, group_id, target, "mute")
        if not role_ok:
            return await self._image_result(event, "禁言结果", [role_msg])
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target[0]),
            duration=duration,
        )
        lines = [f"{target[1] or target[0]} 禁言 {self._human_duration(duration)}", msg]
        return await self._image_result(event, "禁言结果", lines if ok else [msg])

    async def _unmute_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        target = self._extract_target_user(event)
        if not group_id or not target:
            return await self._image_result(event, "解禁", ["用法：解禁 @成员"])
        valid, validation_msg = await self._ensure_group_member(event, group_id, target)
        if not valid:
            return await self._image_result(event, "解禁结果", [validation_msg])
        role_ok, role_msg = await self._ensure_role_action_allowed(event, group_id, target, "mute")
        if not role_ok:
            return await self._image_result(event, "解禁结果", [role_msg])
        ok, msg = await self._onebot_call(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target[0]),
            duration=0,
        )
        return await self._image_result(
            event, "解禁结果", [f"{target[1] or target[0]} 已解禁。", msg] if ok else [msg]
        )

    async def _recall_result(self, event: AstrMessageEvent):
        message_id = self._extract_reply_message_id(event) or self._extract_first_number(event)
        if not message_id:
            return await self._image_result(event, "撤回", ["请回复要撤回的消息，或发送：撤回 消息ID"])
        ok, msg = await self._onebot_call(event, "delete_msg", message_id=int(message_id))
        return await self._image_result(event, "撤回结果", [msg if ok else f"撤回失败：{msg}"])

    async def _batch_recall_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            return await self._image_result(event, "批量撤回", ["这个功能要在群聊里用。"])
        count = max(1, min(self._extract_count(event, default=5), 50))
        target = self._extract_target_user(event)
        ids = self._recent_message_ids(group_id, count, target[0] if target else None, event)
        success = 0
        errors = []
        for msg_id in ids:
            ok, msg = await self._onebot_call(event, "delete_msg", message_id=int(msg_id))
            success += 1 if ok else 0
            if not ok:
                errors.append(msg)
        lines = [f"目标：最近 {count} 条", f"成功撤回：{success} 条"]
        if errors:
            lines.append(f"失败：{errors[0]}")
        return await self._image_result(event, "批量撤回结果", lines)

    async def _list_essence_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            return await self._image_result(event, "查看群精华", ["这个功能要在群聊里用。"])
        ok, data = await self._onebot_call_raw(event, "get_essence_msg_list", group_id=int(group_id))
        if not ok:
            return await self._image_result(event, "查看群精华", [str(data)])
        rows = data if isinstance(data, list) else []
        lines = [f"共 {len(rows)} 条群精华。"] + [
            f"{idx}. {row.get('sender_nick') or row.get('sender_id')}: {row.get('message_id')}"
            for idx, row in enumerate(rows[:10], 1)
        ]
        return await self._image_result(event, "查看群精华", lines)

    async def _set_group_portrait_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        image_url = self._extract_image_url(event)
        if not group_id or not image_url:
            return await self._image_result(event, "设置群头像", ["请带一张图片发送：设置群头像"])
        ok, msg = await self._onebot_call(event, "set_group_portrait", group_id=int(group_id), file=image_url)
        return await self._image_result(event, "设置群头像", [msg])

    async def _set_group_name_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        name = self._clean_command_text(event)
        if not group_id or not name:
            return await self._image_result(event, "设置群名", ["用法：设置群名 新群名"])
        ok, msg = await self._onebot_call(event, "set_group_name", group_id=int(group_id), group_name=name)
        return await self._image_result(event, "设置群名", [msg])

    async def _send_group_notice_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        content = self._clean_command_text(event)
        if not group_id or not content:
            return await self._image_result(event, "发布群公告", ["用法：发布群公告 内容"])
        ok, msg = await self._onebot_call(event, "_send_group_notice", group_id=int(group_id), content=content)
        return await self._image_result(event, "发布群公告", [msg])

    async def _get_group_notice_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            return await self._image_result(event, "查看群公告", ["这个功能要在群聊里用。"])
        ok, data = await self._onebot_call_raw(event, "_get_group_notice", group_id=int(group_id))
        if not ok:
            return await self._image_result(event, "查看群公告", [str(data)])
        rows = data if isinstance(data, list) else []
        lines = [f"共 {len(rows)} 条公告。"] + [str(row.get("message") or row.get("content") or row)[:80] for row in rows[:5]]
        return await self._image_result(event, "查看群公告", lines)

    async def _set_banned_words_result(self, event: AstrMessageEvent):
        words = [item.strip() for item in re.split(r"[，,\s]+", self._clean_command_text(event)) if item.strip()]
        settings = self._group_settings(event)
        settings["banned_words"] = words
        self._save_stats()
        return await self._image_result(event, "设置禁词", [f"已设置 {len(words)} 个禁词。", "为空时表示关闭自定义禁词。"])

    async def _builtin_banned_words_result(self, event: AstrMessageEvent):
        text = self._clean_command_text(event)
        enable = not any(key in text for key in ("关", "关闭", "off", "0", "否"))
        return await self._set_group_setting_result(event, "内置禁词", "builtin_banned_words", enable)

    async def _moderation_result(self, event: AstrMessageEvent, text: str):
        group_id = self._group_id(event)
        user_id = str(event.get_sender_id() or "")
        if not group_id or not user_id or not text or self._looks_like_command(text):
            return None
        settings = self._group_settings(event)
        if self._is_whitelisted(event, user_id):
            return None
        now = int(time.time())

        if settings.get("night_curfew") and 0 <= int(time.strftime("%H", time.localtime(now))) < 6:
            duration = 600
            ok, msg = await self._onebot_call(event, "set_group_ban", group_id=int(group_id), user_id=int(user_id), duration=duration)
            return await self._image_result(event, "宵禁提醒", [f"当前处于宵禁时间，禁言 {self._human_duration(duration)}。", msg])

        words = list(settings.get("banned_words", []))
        if settings.get("builtin_banned_words"):
            words.extend(["广告", "代刷", "博彩", "贷款", "加群"])
        hit = next((word for word in words if word and word in text), None)
        if hit:
            duration = int(settings.get("banned_word_mute_seconds", 600))
            ok, msg = await self._onebot_call(event, "set_group_ban", group_id=int(group_id), user_id=int(user_id), duration=duration)
            return await self._image_result(event, "禁词命中", [f"命中：{hit}", f"禁言：{self._human_duration(duration)}", msg])

        spam_seconds = int(settings.get("spam_mute_seconds", 0) or 0)
        if spam_seconds > 0 and self._is_spamming(group_id, user_id):
            ok, msg = await self._onebot_call(event, "set_group_ban", group_id=int(group_id), user_id=int(user_id), duration=spam_seconds)
            return await self._image_result(event, "刷屏禁言", [f"检测到短时间刷屏。", f"禁言：{self._human_duration(spam_seconds)}", msg])
        return None

    def _looks_like_command(self, text: str) -> bool:
        clean = text.strip().lstrip("/")
        return any(clean.startswith(name) for name in self._command_names())

    def _matched_command_name(self, text: str) -> str | None:
        clean = text.strip().lstrip("/")
        for name in sorted(self._command_names(), key=len, reverse=True):
            if clean == name or clean.startswith(name + " ") or clean.startswith(name + "\u3000"):
                return name
        return None

    async def _speech_rank_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if not group_id:
            return await self._image_result(event, "发言统计", ["这个功能要在群聊里用。"])
        members = self.stats["groups"].get(group_id, {}).get("members", {})
        ranking = sorted(
            members.items(), key=lambda item: item[1].get("count", 0), reverse=True
        )[:10]
        if not ranking:
            lines = ["还没有统计到发言。", "从现在开始我会记录群内发言。"]
        else:
            lines = [
                f"{idx}. {info.get('name') or uid}: {info.get('count', 0)} 条 / {info.get('chars', 0)} 字"
                for idx, (uid, info) in enumerate(ranking, 1)
            ]
        return await self._image_result(event, "本群发言排行", lines)

    async def _my_speech_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        user_id = str(event.get_sender_id())
        info = self.stats["groups"].get(group_id, {}).get("members", {}).get(user_id)
        if not info:
            lines = ["还没有统计到你的发言。", "你刚刚这句话之后就会开始计数。"]
        else:
            last = self._format_time(info.get("last_active"))
            lines = [
                f"昵称：{info.get('name') or event.get_sender_name()}",
                f"发言：{info.get('count', 0)} 条",
                f"字数：{info.get('chars', 0)} 字",
                f"最近：{last}",
            ]
        return await self._image_result(event, "我的发言统计", lines)

    async def _invite_rank_result(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        rows = self.stats["invites"].get(group_id, {})
        ranking = sorted(rows.items(), key=lambda item: item[1].get("count", 0), reverse=True)[:10]
        if not ranking:
            lines = [
                "暂时没有邀请记录。",
                "协议端没上报邀请人时，可以用：记邀请 @邀请人",
            ]
        else:
            lines = [
                f"{idx}. {info.get('name') or uid}: 邀请 {info.get('count', 0)} 人"
                for idx, (uid, info) in enumerate(ranking, 1)
            ]
        return await self._image_result(event, "邀请排行", lines)

    async def _today_partner_result(self, event: AstrMessageEvent, title: str):
        group_id = self._group_id(event)
        members = self.stats["groups"].get(group_id, {}).get("members", {})
        choices = [
            (uid, info.get("name") or uid)
            for uid, info in members.items()
            if str(uid) and str(uid) != str(getattr(event.message_obj, "self_id", ""))
        ]
        if not choices:
            return await self._image_result(event, title, ["群里还没有可抽取成员。", "先让大家发几句话再试试。"])

        today = time.strftime("%Y-%m-%d", time.localtime())
        seed = f"{group_id}:{today}:{title}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        uid, name = choices[int(digest[:8], 16) % len(choices)]
        anime = self._load_random_anime_image(title)
        lines = [
            f"{title}：{name}",
            f"QQ：{uid}",
            "今日缘分已盖章，明天再换。",
        ]
        try:
            img = self._render_today_image(event, title, lines, anime)
            path = self.cache_dir / f"today_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
            img.save(path, "PNG", optimize=True)
            return event.chain_result([Comp.Image.fromFileSystem(str(path))])
        except Exception as exc:
            logger.exception("生成今日抽取图片失败")
            return event.plain_result(f"{title}\n" + "\n".join(lines) + f"\n\n图片生成失败：{exc}")

    async def _render_card(self, event: AstrMessageEvent, title: str, lines: list[str] | str) -> Path:
        if Image is None:
            raise RuntimeError("缺少 Pillow，请安装 requirements.txt 里的 Pillow。")

        if isinstance(lines, str):
            raw_lines = lines.splitlines() or [lines]
        else:
            raw_lines = [str(line) for line in lines]

        if "菜单" in title:
            img = self._render_menu_image(event)
        else:
            img = self._render_reply_image(event, title, raw_lines)

        path = self.cache_dir / f"reply_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        img.save(path, "PNG", optimize=True)
        return path

    def _render_menu_image(self, event: AstrMessageEvent):
        width, height = 980, 980
        img = self._load_reply_background(width, height) or Image.new("RGBA", (width, height), "#fff1a6")
        draw = ImageDraw.Draw(img)

        panel = (34, 34, width - 34, height - 34)
        draw.rounded_rectangle(panel, radius=38, outline="#7e5c3a", width=4)
        self._draw_corner_sparkles(draw, width, height)

        font_title = self._font(48, bold=True)
        font_body = self._font(22, bold=True)
        font_small = self._font(21)
        draw.text((82, 72), "懒羊羊群管菜单", font=font_title, fill="#7b2638", stroke_width=2, stroke_fill="#fff3cd")
        self._draw_sender_badge(img, draw, event, 575, 60)

        left_box = (70, 174, 470, height - 86)
        right_box = (510, 174, 910, height - 86)
        for box in (left_box, right_box):
            draw.rounded_rectangle(box, radius=28, fill=(255, 254, 246, 232), outline="#efd27a", width=3)

        lines = self._menu_lines()
        midpoint = (len(lines) + 1) // 2
        self._draw_menu_lines(draw, left_box, lines[:midpoint], font_body)
        self._draw_menu_lines(draw, right_box, lines[midpoint:], font_body)

        draw.text((68, height - 62), "所有命令回复都会生成懒羊羊背景图片", font=font_small, fill="#8f5a3b", stroke_width=1, stroke_fill="#fff3cd")
        return img.convert("RGB")

    def _draw_menu_lines(self, draw: Any, box: tuple[int, int, int, int], lines: list[str], font: Any):
        x = box[0] + 28
        y = box[1] + 24
        max_width = box[2] - x - 24
        for line in lines:
            wrapped = self._wrap_text_to_width(draw, line, font, max_width)
            for part in wrapped:
                draw.text((x, y), part, font=font, fill="#6c3040")
                y += 30
            y += 6

    def _render_reply_image(self, event: AstrMessageEvent, title: str, raw_lines: list[str]):
        width = 980
        line_height = 48
        font_title = self._font(46, bold=True)
        font_body = self._font(30, bold=True)
        font_small = self._font(22)

        probe = Image.new("RGBA", (1, 1))
        probe_draw = ImageDraw.Draw(probe)
        text_max_width = 548 - 110 - 28
        body_lines = []
        for line in raw_lines:
            body_lines.extend(
                self._wrap_text_to_width(probe_draw, line, font_body, text_max_width)
                or [""]
            )
        height = max(620, 320 + len(body_lines) * line_height)

        background = self._load_reply_background(width, height)
        img = background or Image.new("RGBA", (width, height), "#fff1a6")
        draw = ImageDraw.Draw(img)
        if background:
            panel = (36, 34, width - 36, height - 34)
            draw.rounded_rectangle(panel, radius=38, outline="#7e5c3a", width=4)
            self._draw_corner_sparkles(draw, width, height)
        else:
            panel = (26, 26, width - 26, height - 26)
            draw.rounded_rectangle(panel, radius=38, fill="#ffe493", outline="#9c7441", width=4)
            self._draw_checker_pattern(draw, panel, cell=34)
            self._draw_corner_sparkles(draw, width, height)
            self._draw_sheep_car(draw, 232, min(335, height - 190), scale=0.82)
            self._draw_mini_sheep(draw, 835, 110, scale=0.82)

        title_x = 92 if background else 460
        draw.text((title_x, 70), title, font=font_title, fill="#7b2638", stroke_width=2, stroke_fill="#fff3cd")
        self._draw_sender_badge(img, draw, event, title_x, 122)

        box_top = 230
        box_bottom = height - 76
        box = (78, box_top, 548, box_bottom) if background else (430, box_top, 875, box_bottom)
        text_x = 110 if background else 462
        fill = (255, 254, 246, 228) if background else "#fffef6"
        if not background:
            draw.rounded_rectangle(box, radius=28, fill=fill, outline="#efd27a", width=3)
        y = box_top + 26
        for line in body_lines:
            if background:
                draw.text((text_x, y), line, font=font_body, fill="#6c3040", stroke_width=2, stroke_fill="#fff3cd")
            else:
                draw.text((text_x, y), line, font=font_body, fill="#6c3040")
            y += line_height

        draw.text((68, height - 62), "懒羊羊主题卡片回复", font=font_small, fill="#8f5a3b")
        return img.convert("RGB")

    def _render_today_image(self, event: AstrMessageEvent, title: str, lines: list[str], anime: Any = None):
        width, height = 980, 620
        if anime:
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            img = ImageOps.fit(anime.convert("RGBA"), (width, height), method=resample, centering=(0.5, 0.35))
            veil = Image.new("RGBA", (width, height), (24, 16, 28, 74))
            img = Image.alpha_composite(img, veil)
        else:
            img = Image.new("RGBA", (width, height), "#1f1b2e")
        draw = ImageDraw.Draw(img)
        panel = (34, 34, width - 34, height - 34)
        draw.rounded_rectangle(panel, radius=38, outline="#f5d67b", width=4)
        self._draw_corner_sparkles(draw, width, height)

        if anime:
            portrait = ImageOps.fit(anime.convert("RGBA"), (330, 410), method=resample, centering=(0.5, 0.35))
            mask = Image.new("L", portrait.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, portrait.width, portrait.height), radius=28, fill=255)
            img.paste(portrait, (590, 144), mask)
            draw.rounded_rectangle((590, 144, 920, 554), radius=28, outline="#efd27a", width=4)

        font_title = self._font(48, bold=True)
        font_body = self._font(31, bold=True)
        font_small = self._font(22)
        draw.text((86, 76), title, font=font_title, fill="#fff3cd", stroke_width=2, stroke_fill="#5a2030")
        self._draw_sender_badge(img, draw, event, 88, 132, dark=True)

        box = (76, 230, 548, 502)
        draw.rounded_rectangle(box, radius=28, fill=(255, 254, 246, 226), outline="#efd27a", width=3)
        y = 260
        for line in lines:
            for part in self._wrap_text(line, max_chars=16):
                draw.text((108, y), part, font=font_body, fill="#6c3040")
                y += 48
            y += 8
        draw.text((70, height - 60), "今日缘分卡 | 二次元随机图", font=font_small, fill="#fff3cd", stroke_width=1, stroke_fill="#5a2030")
        return img.convert("RGB")

    def _load_random_anime_image(self, key: str):
        anime_dir = self.base_dir / "assets" / "anime"
        all_files = sorted(
            path for path in anime_dir.glob("*")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
        if "老公" in key:
            files = [path for path in all_files if path.name.startswith(("inuyasha_1", "parasyte_"))]
        elif "老婆" in key:
            files = [path for path in all_files if path.name.startswith(("edgerunners_", "inuyasha_2"))]
        else:
            files = all_files
        if not files:
            return None
        today = time.strftime("%Y-%m-%d", time.localtime())
        digest = hashlib.sha256(f"{today}:{key}:anime".encode("utf-8")).hexdigest()
        path = files[int(digest[:8], 16) % len(files)]
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            logger.exception("加载二次元图片失败：%s", path)
            return None

    def _load_reply_background(self, width: int, height: int):
        path = self.base_dir / "assets" / "lanyangyang_reply_bg.png"
        if not path.exists():
            return None
        try:
            with Image.open(path) as raw:
                image = raw.convert("RGBA")
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            return ImageOps.fit(image, (width, height), method=resample, centering=(0.5, 1.0))
        except Exception:
            logger.exception("加载懒羊羊背景图失败，已使用默认绘制背景")
            return None

    def _draw_checker_pattern(self, draw: Any, box: tuple[int, int, int, int], cell: int = 34):
        left, top, right, bottom = box
        for y in range(top, bottom, cell):
            for x in range(left, right, cell):
                fill = "#ffd36f" if ((x // cell) + (y // cell)) % 2 == 0 else "#fff3bd"
                draw.rectangle((x, y, min(x + cell, right), min(y + cell, bottom)), fill=fill)
        draw.rounded_rectangle(box, radius=38, outline="#9c7441", width=4)

    def _draw_corner_sparkles(self, draw: Any, width: int, height: int):
        color = "#fff9df"
        for x, y, r in [(74, 62, 6), (108, 92, 4), (900, 82, 5), (870, 500, 5), (122, height - 82, 4)]:
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        for x, y in [(160, 92), (430, 108), (402, 452), (904, 398)]:
            draw.line((x - 10, y, x + 10, y), fill=color, width=3)
            draw.line((x, y - 10, x, y + 10), fill=color, width=3)

    def _draw_menu_pill(self, draw: Any, x: int, y: int, w: int, h: int, idx: int, label: str, font: Any):
        shadow = (x + 5, y + 6, x + w + 5, y + h + 6)
        draw.rounded_rectangle(shadow, radius=h // 2, fill="#e4b75e")
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill="#fffdf8", outline="#efc96d", width=3)
        draw.ellipse((x + 16, y + 10, x + 48, y + 42), fill="#ffd971", outline="#efbd55", width=2)
        self._draw_centered_text(draw, (x + 62, y, x + w, y + h), label, font, "#713044")
        self._draw_centered_text(draw, (x + 16, y + 10, x + 48, y + 42), str(idx), self._font(18, bold=True), "#fffdf8")

    def _draw_sheep_car(self, draw: Any, cx: int, cy: int, scale: float = 1.0):
        def s(value: int) -> int:
            return int(value * scale)

        x, y = cx, cy
        outline = "#8a6147"
        draw.rounded_rectangle((x - s(150), y - s(18), x + s(150), y + s(76)), radius=s(42), fill="#f4c35f", outline=outline, width=s(5))
        draw.pieslice((x - s(118), y - s(110), x + s(82), y + s(74)), 185, 358, fill="#ffe59d", outline=outline, width=s(5))
        draw.ellipse((x - s(133), y + s(50), x - s(74), y + s(108)), fill="#6a5b55", outline=outline, width=s(4))
        draw.ellipse((x + s(82), y + s(50), x + s(141), y + s(108)), fill="#6a5b55", outline=outline, width=s(4))
        draw.ellipse((x - s(116), y + s(63), x - s(90), y + s(89)), fill="#f8df9a")
        draw.ellipse((x + s(99), y + s(63), x + s(125), y + s(89)), fill="#f8df9a")
        draw.ellipse((x - s(161), y + s(3), x - s(125), y + s(39)), fill="#f48f4e", outline=outline, width=s(3))
        draw.ellipse((x + s(121), y + s(3), x + s(157), y + s(39)), fill="#f48f4e", outline=outline, width=s(3))
        draw.rounded_rectangle((x - s(42), y + s(66), x + s(64), y + s(92)), radius=s(8), fill="#fff6cf", outline=outline, width=s(3))

        duck_x, duck_y = x - s(95), y - s(60)
        draw.ellipse((duck_x - s(26), duck_y - s(16), duck_x + s(26), duck_y + s(34)), fill="#ffd34f", outline=outline, width=s(3))
        draw.ellipse((duck_x - s(17), duck_y - s(43), duck_x + s(19), duck_y - s(7)), fill="#ffe06b", outline=outline, width=s(3))
        draw.polygon([(duck_x + s(16), duck_y - s(24)), (duck_x + s(42), duck_y - s(15)), (duck_x + s(16), duck_y - s(5))], fill="#f08c44", outline=outline)
        draw.ellipse((duck_x - s(5), duck_y - s(27), duck_x + s(2), duck_y - s(20)), fill="#5d4a3f")

        sheep_x, sheep_y = x - s(12), y - s(120)
        self._draw_sheep_head(draw, sheep_x, sheep_y, scale=scale * 1.25, mouth_open=True)
        draw.rounded_rectangle((sheep_x - s(55), sheep_y + s(76), sheep_x + s(52), sheep_y + s(145)), radius=s(28), fill="#fff7df", outline=outline, width=s(4))
        draw.arc((sheep_x - s(92), sheep_y + s(50), sheep_x - s(32), sheep_y + s(128)), 100, 260, fill=outline, width=s(5))
        draw.arc((sheep_x + s(30), sheep_y + s(50), sheep_x + s(90), sheep_y + s(128)), -80, 80, fill=outline, width=s(5))

    def _draw_mini_sheep(self, draw: Any, x: int, y: int, scale: float = 1.0):
        self._draw_sheep_head(draw, x, y, scale=scale, mouth_open=False)
        r = int(38 * scale)
        draw.rounded_rectangle((x - r, y + int(40 * scale), x + r, y + int(92 * scale)), radius=int(20 * scale), fill="#fff7df", outline="#8a6147", width=max(2, int(3 * scale)))
        draw.ellipse((x - int(20 * scale), y + int(57 * scale), x - int(8 * scale), y + int(69 * scale)), fill="#8a6147")
        draw.ellipse((x + int(8 * scale), y + int(57 * scale), x + int(20 * scale), y + int(69 * scale)), fill="#8a6147")

    def _draw_sheep_head(self, draw: Any, x: int, y: int, scale: float = 1.0, mouth_open: bool = False):
        def s(value: int) -> int:
            return int(value * scale)

        outline = "#8a6147"
        wool = "#fffdf6"
        for dx, dy, r in [(-40, -28, 26), (-10, -45, 30), (26, -42, 28), (52, -18, 24), (-55, 6, 25), (-30, 26, 27), (20, 24, 29), (55, 8, 24)]:
            draw.ellipse((x + s(dx - r), y + s(dy - r), x + s(dx + r), y + s(dy + r)), fill=wool, outline=outline, width=max(2, s(3)))
        draw.rounded_rectangle((x - s(45), y - s(16), x + s(45), y + s(62)), radius=s(28), fill="#ffe8c6", outline=outline, width=max(2, s(4)))
        draw.polygon([(x - s(62), y - s(16)), (x - s(92), y - s(38)), (x - s(76), y + s(10))], fill="#7d513d", outline=outline)
        draw.polygon([(x + s(62), y - s(16)), (x + s(92), y - s(38)), (x + s(76), y + s(10))], fill="#7d513d", outline=outline)
        draw.ellipse((x - s(20), y + s(16), x - s(10), y + s(27)), fill="#5a4338")
        draw.ellipse((x + s(12), y + s(16), x + s(22), y + s(27)), fill="#5a4338")
        draw.ellipse((x - s(5), y + s(30), x + s(8), y + s(39)), fill="#f08b80")
        if mouth_open:
            draw.ellipse((x - s(18), y + s(40), x + s(20), y + s(78)), fill="#7b2638", outline=outline, width=max(2, s(3)))
            draw.ellipse((x - s(8), y + s(57), x + s(14), y + s(75)), fill="#f28b8e")
        else:
            draw.arc((x - s(16), y + s(36), x + s(18), y + s(56)), 15, 165, fill=outline, width=max(2, s(3)))

    def _draw_centered_text(self, draw: Any, box: tuple[int, int, int, int], text: str, font: Any, fill: str):
        left, top, right, bottom = box
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = left + (right - left - text_w) / 2
        y = top + (bottom - top - text_h) / 2 - 2
        draw.text((x, y), text, font=font, fill=fill)

    def _draw_sheep_badge(self, draw: Any, x: int, y: int):
        wool = "#ffffff"
        outline = "#7a9d68"
        for dx, dy, r in [(-38, -22, 30), (0, -36, 34), (38, -22, 30), (-24, 8, 34), (24, 8, 34)]:
            draw.ellipse((x + dx - r, y + dy - r, x + dx + r, y + dy + r), fill=wool, outline=outline, width=3)
        draw.rounded_rectangle((x - 44, y - 8, x + 44, y + 58), radius=28, fill="#ffe8ad", outline=outline, width=3)
        draw.arc((x - 70, y - 8, x - 26, y + 48), 90, 280, fill="#c7a15a", width=5)
        draw.arc((x + 26, y - 8, x + 70, y + 48), -100, 90, fill="#c7a15a", width=5)
        draw.ellipse((x - 20, y + 16, x - 12, y + 24), fill="#4f4a37")
        draw.ellipse((x + 12, y + 16, x + 20, y + 24), fill="#4f4a37")
        draw.arc((x - 14, y + 26, x + 14, y + 44), 15, 165, fill="#8a6b39", width=3)

    def _font(self, size: int, bold: bool = False):
        candidates = [
            str(self.base_dir / "assets" / "fonts" / "wqy-microhei.ttc"),
            str(self.base_dir / "assets" / "fonts" / "NotoSansCJKsc-Regular.otf"),
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for item in candidates:
            if item and Path(item).exists():
                return ImageFont.truetype(item, size=size)
        return ImageFont.load_default()

    def _wrap_text(self, text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        lines = []
        current = ""
        for char in text:
            current += char
            if len(current) >= max_chars:
                lines.append(current)
                current = ""
        if current:
            lines.append(current)
        return lines

    def _wrap_text_to_width(self, draw: Any, text: str, font: Any, max_width: int) -> list[str]:
        text = str(text)
        if not text:
            return [""]
        lines: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if current and bbox[2] - bbox[0] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    def _load_avatar(self, user_id: str):
        if not user_id:
            return None
        cache = self.cache_dir / f"avatar_{user_id}.png"
        try:
            if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
                raw = cache.read_bytes()
            else:
                url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
                with urlopen(url, timeout=4) as resp:
                    raw = resp.read()
                cache.write_bytes(raw)
            avatar = Image.open(BytesIO(raw)).convert("RGBA").resize((80, 80))
            mask = Image.new("L", (80, 80), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, 80, 80), fill=255)
            avatar.putalpha(mask)
            return ImageOps.expand(avatar, border=4, fill="#ffffff")
        except Exception:
            return None

    def _draw_sender_badge(self, img: Any, draw: Any, event: AstrMessageEvent, x: int, y: int, dark: bool = False):
        user_id = str(event.get_sender_id() or "")
        name = event.get_sender_name() or user_id or "未知用户"
        shown_name = name if len(name) <= 8 else f"{name[:7]}..."
        avatar = self._load_avatar(user_id)
        if avatar:
            img.paste(avatar, (x, y), avatar)
        else:
            draw.ellipse((x, y, x + 88, y + 88), fill="#fffdf6", outline="#efd27a", width=4)
            self._draw_centered_text(
                draw,
                (x + 8, y + 8, x + 80, y + 80),
                "头像",
                self._font(18, bold=True),
                "#8f5a3b",
            )

        name_fill = "#fff3cd" if dark else "#7b2638"
        id_fill = "#f5d67b" if dark else "#8f5a3b"
        stroke = "#5a2030" if dark else "#fff3cd"
        draw.text(
            (x + 104, y + 10),
            f"呼叫人：{shown_name}",
            font=self._font(24, bold=True),
            fill=name_fill,
            stroke_width=1,
            stroke_fill=stroke,
        )
        if user_id:
            draw.text(
                (x + 106, y + 48),
                f"QQ：{user_id}",
                font=self._font(19),
                fill=id_fill,
                stroke_width=1 if dark else 0,
                stroke_fill=stroke,
            )

    async def _record_message(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        user_id = str(event.get_sender_id() or "")
        if not group_id or not user_id:
            return
        group = self.stats["groups"].setdefault(group_id, {"members": {}, "history": []})
        member = group["members"].setdefault(user_id, {"name": "", "count": 0, "chars": 0, "last_active": 0})
        member["name"] = event.get_sender_name() or member.get("name") or user_id
        member["count"] = int(member.get("count", 0)) + 1
        member["chars"] = int(member.get("chars", 0)) + len(event.message_str or "")
        member["last_active"] = int(time.time())
        message_id = getattr(event.message_obj, "message_id", None)
        if message_id:
            group["history"].append(
                {
                    "message_id": str(message_id),
                    "user_id": user_id,
                    "name": member["name"],
                    "text": (event.message_str or "")[:120],
                    "time": int(time.time()),
                }
            )
            group["history"] = group["history"][-300:]
        self._save_stats()

    async def _record_invite_from_raw(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_increase":
            return
        group_id = str(raw.get("group_id") or self._group_id(event))
        operator_id = str(raw.get("operator_id") or "")
        if not group_id or not operator_id:
            return
        self._add_invite(group_id, operator_id, raw.get("operator_id"))
        self._save_stats()

    def _add_invite(self, group_id: str, user_id: str, name: Any = None):
        rows = self.stats["invites"].setdefault(group_id, {})
        info = rows.setdefault(str(user_id), {"name": "", "count": 0, "last_time": 0})
        info["name"] = str(name or info.get("name") or user_id)
        info["count"] = int(info.get("count", 0)) + 1
        info["last_time"] = int(time.time())

    def _load_stats(self) -> dict:
        if not self.data_file.exists():
            return {"groups": {}, "invites": {}, "daily": {}, "settings": {}, "votes": {}}
        try:
            data = json.loads(self.data_file.read_text(encoding="utf-8"))
            data.setdefault("groups", {})
            data.setdefault("invites", {})
            data.setdefault("daily", {})
            data.setdefault("settings", {})
            data.setdefault("votes", {})
            return data
        except Exception:
            logger.exception("读取懒羊羊统计数据失败，已重新初始化。")
            return {"groups": {}, "invites": {}, "daily": {}, "settings": {}, "votes": {}}

    def _save_stats(self):
        self.data_file.write_text(
            json.dumps(self.stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _onebot_call(self, event: AstrMessageEvent, action: str, **payload):
        if event.get_platform_name() != "aiocqhttp" or not hasattr(event, "bot"):
            return False, "当前平台不是 aiocqhttp/OneBot，不能执行这个群管动作。"
        try:
            await event.bot.api.call_action(action, **payload)
            return True, "操作完成。"
        except Exception as exc:
            logger.exception("OneBot API 调用失败：%s", action)
            return False, self._format_onebot_error(exc)

    async def _onebot_call_raw(self, event: AstrMessageEvent, action: str, **payload):
        if event.get_platform_name() != "aiocqhttp" or not hasattr(event, "bot"):
            return False, "当前平台不是 aiocqhttp/OneBot，不能执行这个群管动作。"
        try:
            return True, await event.bot.api.call_action(action, **payload)
        except Exception as exc:
            logger.exception("OneBot API 调用失败：%s", action)
            return False, self._format_onebot_error(exc)

    def _format_onebot_error(self, exc: Exception) -> str:
        retcode = getattr(exc, "retcode", None)
        wording = getattr(exc, "wording", None) or getattr(exc, "message", None)
        text = str(wording or exc).strip()
        if not text:
            text = exc.__class__.__name__
        if retcode:
            return f"OneBot 返回失败：{text}（retcode={retcode}）"
        return f"OneBot 返回失败：{text}"

    async def _ensure_group_member(
        self,
        event: AstrMessageEvent,
        group_id: str,
        target: tuple[str, str],
    ) -> tuple[bool, str]:
        ok, info = await self._onebot_call_raw(
            event,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(target[0]),
            no_cache=True,
        )
        if ok:
            nickname = ""
            if isinstance(info, dict):
                nickname = str(info.get("card") or info.get("nickname") or "")
            display = nickname or target[1] or target[0]
            return True, f"目标确认：{display}（{target[0]}）"
        return False, (
            f"QQ {target[0]} 不在本群，已停止执行，未进行群管操作。"
            "请确认 QQ 号，或使用 @成员。"
        )

    async def _ensure_role_action_allowed(
        self,
        event: AstrMessageEvent,
        group_id: str,
        target: tuple[str, str],
        action: str,
    ) -> tuple[bool, str]:
        ok, info = await self._onebot_call_raw(
            event,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(target[0]),
            no_cache=True,
        )
        if not ok or not isinstance(info, dict):
            return False, (
                f"无法确认 QQ {target[0]} 的群权限，已停止执行，未进行群管操作。"
                "请确认目标仍在本群。"
            )
        role = str(info.get("role") or "")
        display = str(info.get("card") or info.get("nickname") or target[1] or target[0])
        if role == "owner":
            action_name = {
                "kick": "踢出/踢黑",
                "mute": "禁言/解禁",
                "admin": "上管/下管",
            }.get(action, "群管")
            return False, f"{display}（{target[0]}）是群主，不能执行{action_name}。"
        return True, f"权限确认：{display}（{target[0]}）role={role or 'unknown'}"

    async def _send_onebot_message(self, event: AstrMessageEvent, message: str):
        group_id = self._group_id(event)
        if group_id:
            return await self._onebot_call(event, "send_group_msg", group_id=int(group_id), message=message)
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return False, "无法获取接收者。"
        return await self._onebot_call(event, "send_private_msg", user_id=int(user_id), message=message)

    def _group_settings(self, event: AstrMessageEvent) -> dict:
        group_id = self._group_id(event) or "private"
        settings = self.stats.setdefault("settings", {}).setdefault(group_id, {})
        settings.setdefault("banned_words", [])
        settings.setdefault("banned_word_mute_seconds", 600)
        settings.setdefault("builtin_banned_words", False)
        settings.setdefault("spam_mute_seconds", 0)
        settings.setdefault("night_curfew", False)
        settings.setdefault("join_approval", False)
        settings.setdefault("whitelist", {})
        return settings

    def _whitelist_map(self, settings: dict) -> dict[str, str]:
        value = settings.setdefault("whitelist", {})
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            converted = {str(uid): str(uid) for uid in value}
            settings["whitelist"] = converted
            return converted
        settings["whitelist"] = {}
        return settings["whitelist"]

    def _is_whitelisted(self, event: AstrMessageEvent, user_id: str) -> bool:
        settings = self._group_settings(event)
        return str(user_id) in self._whitelist_map(settings)

    def _extract_target_user(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        bot_id = str(getattr(event.message_obj, "self_id", "") or "")
        for item in event.get_messages():
            qq = getattr(item, "qq", None)
            if qq and str(qq) not in {bot_id, "all"}:
                return str(qq), self._member_name_from_stats(event, str(qq))
        text = self._command_args(event)
        numbers = re.findall(r"\b\d{5,12}\b", text)
        if numbers:
            return numbers[0], self._member_name_from_stats(event, numbers[0])
        return None

    def _extract_reply_message_id(self, event: AstrMessageEvent) -> str | None:
        for item in event.get_messages():
            name = item.__class__.__name__.lower()
            if name == "reply":
                for attr in ("id", "message_id", "seq"):
                    value = getattr(item, attr, None)
                    if value:
                        return str(value)
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            reply = raw.get("reply") or raw.get("source")
            if isinstance(reply, dict):
                return str(reply.get("message_id") or reply.get("id") or "") or None
        return None

    def _extract_duration(self, event: AstrMessageEvent, default_seconds: int) -> int:
        text = self._command_args(event)
        target = self._extract_target_user(event)
        matches = list(re.finditer(r"(\d+)\s*(秒|s|分钟|分|m|小时|时|h|天|d)?", text, re.I))
        if not matches:
            return default_seconds
        match = None
        for item in reversed(matches):
            number = item.group(1)
            unit_text = item.group(2)
            if target and number == str(target[0]):
                continue
            if unit_text or int(number) < 10000:
                match = item
                break
        if not match:
            return default_seconds
        value = int(match.group(1))
        unit = (match.group(2) or "m").lower()
        if unit in {"秒", "s"}:
            return value
        if unit in {"小时", "时", "h"}:
            return value * 3600
        if unit in {"天", "d"}:
            return value * 86400
        return value * 60

    def _extract_count(self, event: AstrMessageEvent, default: int) -> int:
        text = self._command_args(event)
        nums = re.findall(r"\b\d{1,3}\b", text)
        return int(nums[-1]) if nums else default

    def _extract_first_number(self, event: AstrMessageEvent) -> str | None:
        nums = re.findall(r"\b\d+\b", self._command_args(event))
        return nums[0] if nums else None

    def _clean_command_text(self, event: AstrMessageEvent) -> str:
        text = self._command_args(event)
        text = re.sub(r"\[CQ:[^\]]+\]", " ", text)
        text = re.sub(r"\b\d{5,12}\b", " ", text)
        for item in event.get_messages():
            qq = getattr(item, "qq", None)
            if qq:
                text = text.replace(str(qq), " ")
        return re.sub(r"\s+", " ", text).strip()

    def _extract_image_url(self, event: AstrMessageEvent) -> str | None:
        for item in event.get_messages():
            for attr in ("url", "file", "path"):
                value = getattr(item, attr, None)
                if value and item.__class__.__name__.lower() in {"image", "picture"}:
                    return str(value)
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, str):
            match = re.search(r"\[CQ:image,[^\]]*(?:url|file)=([^,\]]+)", raw)
            if match:
                return match.group(1)
        return None

    def _voice_files(self) -> list[Path]:
        suffixes = {".wav", ".mp3", ".amr", ".silk"}
        return sorted(path for path in self.voice_dir.glob("*") if path.suffix.lower() in suffixes)

    def _parse_music_request(self, text: str) -> tuple[str, str | None]:
        source = "qq"
        raw = text.strip()
        lowered = raw.lower()
        if any(key in lowered for key in ("网易", "163", "netease")):
            source = "163"
        elif "qq" in lowered:
            source = "qq"

        url_match = re.search(r"https?://\S+", raw)
        if url_match:
            parsed = urlparse(url_match.group(0))
            host = parsed.netloc.lower()
            if "163.com" in host:
                source = "163"
                song_id = parse_qs(parsed.query).get("id", [None])[0]
                if song_id:
                    return source, song_id
            if "qq.com" in host:
                source = "qq"

        id_match = re.search(r"\b\d{3,20}\b", raw)
        return source, id_match.group(0) if id_match else None

    def _extract_music_query(self, text: str, source: str) -> str:
        query = re.sub(r"https?://\S+", " ", text or "")
        query = re.sub(r"\b\d{3,20}\b", " ", query)
        query = re.sub(r"\b(?:qq|QQ|163|netease)\b", " ", query)
        query = query.replace("网易云音乐", " ").replace("网易云", " ").replace("网易", " ")
        query = query.replace("QQ音乐", " ").replace("音乐", " ")
        return re.sub(r"\s+", " ", query).strip()

    async def _search_music_song(self, source: str, query: str) -> tuple[str, str, str, str] | None:
        if not query:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._search_music_song_sync, source, query)

    def _search_music_song_sync(self, source: str, query: str) -> tuple[str, str, str, str] | None:
        search_order = [source] + [item for item in ("163", "qq") if item != source]
        for item in search_order:
            try:
                result = self._search_netease_song(query) if item == "163" else self._search_qq_song(query)
                if result:
                    return result
            except Exception:
                logger.exception("搜索音乐失败：%s %s", item, query)
        return None

    def _search_netease_song(self, query: str) -> tuple[str, str, str, str] | None:
        body = urlencode({"s": query, "type": "1", "limit": "5", "offset": "0"}).encode("utf-8")
        req = Request(
            "https://music.163.com/api/search/get/web",
            data=body,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://music.163.com/search/",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        data = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return None
        song = songs[0]
        artists = " / ".join(str(item.get("name") or "") for item in song.get("artists", []) if item.get("name"))
        return "163", str(song.get("id")), str(song.get("name") or query), artists

    def _search_qq_song(self, query: str) -> tuple[str, str, str, str] | None:
        url = "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?" + urlencode(
            {"key": query, "format": "json", "inCharset": "utf8", "outCharset": "utf-8"}
        )
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"})
        data = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
        songs = data.get("data", {}).get("song", {}).get("itemlist", [])
        if not songs:
            return None
        song = songs[0]
        song_id = str(song.get("mid") or song.get("id") or "")
        if not song_id:
            return None
        return "qq", song_id, str(song.get("name") or query), str(song.get("singer") or "")

    def _recent_message_ids(
        self,
        group_id: str,
        count: int,
        user_id: str | None,
        event: AstrMessageEvent,
    ) -> list[str]:
        current = str(getattr(event.message_obj, "message_id", "") or "")
        history = self.stats["groups"].get(group_id, {}).get("history", [])
        ids = []
        for row in reversed(history):
            if str(row.get("message_id")) == current:
                continue
            if user_id and str(row.get("user_id")) != str(user_id):
                continue
            ids.append(str(row.get("message_id")))
            if len(ids) >= count:
                break
        return ids

    def _is_spamming(self, group_id: str, user_id: str) -> bool:
        now = int(time.time())
        history = self.stats["groups"].get(group_id, {}).get("history", [])
        recent = [
            row for row in reversed(history)
            if str(row.get("user_id")) == str(user_id) and now - int(row.get("time", 0)) <= 10
        ][:6]
        if len(recent) >= 5:
            return True
        texts = [str(row.get("text", "")) for row in recent if row.get("text")]
        return len(texts) >= 3 and len(set(texts[:3])) == 1

    def _command_args(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        text = text.lstrip("/")
        names = self._command_names()
        for name in sorted(names, key=len, reverse=True):
            if text.startswith(name):
                return text[len(name) :].strip()
        return text

    def _command_names(self) -> list[str]:
        return [
            "菜单",
            "帮助",
            "懒羊羊菜单",
            "今日老公",
            "抽老公",
            "今日老婆",
            "抽老婆",
            "今日小三",
            "抽小三",
            "发言统计",
            "统计",
            "水群排行",
            "我的统计",
            "我水了多少",
            "邀请排行",
            "邀请榜",
            "邀请统计",
            "记邀请",
            "登记邀请",
            "语音",
            "懒羊羊语音",
            "发语音",
            "点歌",
            "音乐",
            "来首歌",
            "禁言",
            "闭嘴",
            "安静",
            "禁我",
            "解禁",
            "解除禁言",
            "开启全禁",
            "关闭全禁",
            "全禁",
            "解除全禁",
            "改名",
            "设置群名片",
            "改我",
            "头衔",
            "改头衔",
            "申请头衔",
            "撤回",
            "删",
            "批量撤回",
            "批撤",
            "踢出群",
            "踢出",
            "踢了",
            "踢",
            "踢黑",
            "拉黑踢出",
            "拉黑",
            "上管",
            "授权",
            "发权",
            "下管",
            "白名单",
            "查白",
            "查看白名单",
            "拉白",
            "加白",
            "加入白名单",
            "删白",
            "移白",
            "取消白名单",
            "设精",
            "设置精华",
            "移精",
            "移除精华",
            "查看群精华",
            "设置群头像",
            "设置群名",
            "发布群公告",
            "查看群公告",
            "禁词禁言",
            "设置禁词",
            "内置禁词",
            "刷屏禁言",
            "投票禁言",
            "赞同禁言",
            "反对禁言",
            "开启宵禁",
            "关闭宵禁",
            "进群审核",
        ]

    def _member_name_from_stats(self, event: AstrMessageEvent, user_id: str) -> str:
        group_id = self._group_id(event)
        return (
            self.stats["groups"]
            .get(group_id, {})
            .get("members", {})
            .get(str(user_id), {})
            .get("name", str(user_id))
        )

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = getattr(event.message_obj, "group_id", "")
        return str(group_id or "")

    def _should_lazy_voice(self, event: AstrMessageEvent, text: str) -> bool:
        if not text:
            return False
        direct = any(key in text for key in self._list_config("voice_trigger_keywords", ["懒羊羊回家"]))
        mentioned = self._is_bot_mentioned(event)
        if not direct and not mentioned:
            return False
        chance = float(self.config.get("voice_reply_chance", 0.35 if direct else 0.12))
        return random.random() <= max(0.0, min(chance, 1.0))

    async def _lazy_voice_or_card(self, event: AstrMessageEvent):
        voices = self._voice_files()
        if voices:
            path = str(random.choice(voices))
            if hasattr(Comp.Record, "fromFileSystem"):
                record = Comp.Record.fromFileSystem(path)
            else:
                record = Comp.Record(file=path, url=path)
            return event.chain_result([record])
        roasts = self._list_config(
            "voice_fallback_roasts",
            [
                "叫我回家？我才刚躺下。",
                "你先回，我再睡五分钟。",
                "别催，懒羊羊正在缓慢加载。",
            ],
        )
        return await self._image_result(event, "懒羊羊语音", [random.choice(roasts), "把 wav / mp3 / amr / silk 放进 voices 目录后，我就能发语音了。"])

    def _is_bot_mentioned(self, event: AstrMessageEvent) -> bool:
        bot_id = str(getattr(event.message_obj, "self_id", "") or "")
        if not bot_id:
            return False
        for item in event.get_messages():
            if str(getattr(item, "qq", "")) == bot_id:
                return True
        return False

    def _is_text_only_chain(self, chain: list[Any]) -> bool:
        return all(item.__class__.__name__ == "Plain" for item in chain)

    def _should_pass_to_other_plugin(self, text: str) -> bool:
        command = text.strip().lstrip("/")
        return command.startswith(("闲鱼", "voice_call", "VoiceCall"))

    def _component_text(self, item: Any) -> str:
        return str(getattr(item, "text", getattr(item, "message", "")) or "")

    def _menu_lines(self) -> list[str]:
        return [
            "菜单 / 发言统计 / 我的统计",
            "今日老公 / 今日老婆 / 今日小三",
            "邀请排行 / 邀请统计 / 记邀请 @邀请人",
            "点歌 qq 123456 / 语音 文件名",
            "禁言 <秒数> @群友 / 禁我 <秒数>",
            "解禁 @群友 / 开启全禁 / 关闭全禁",
            "改名 xxx @群友 / 改我 xxx",
            "头衔 xxx @群友 / 申请头衔 xxx",
            "踢了 @群友 / 拉黑 @群友",
            "上管 @群友 / 下管 @群友 / 白名单",
            "拉白 @群友 / 删白 @群友",
            "回复消息：设精 / 移精 / 撤回",
            "查看群精华 / 设置群头像",
            "设置群名 xxx / 发布群公告 xxx",
            "查看群公告 / 批量撤回 5",
            "禁词禁言 <秒数> / 设置禁词",
            "内置禁词 开/关 / 刷屏禁言 <秒数>",
            "投票禁言 <秒数> @群友",
            "赞同禁言 / 反对禁言",
            "开启宵禁 / 关闭宵禁 / 进群审核",
            "喊“懒羊羊回家”会偶尔语音/图片回怼",
        ]

    def _list_config(self, key: str, default: list[str]) -> list[str]:
        value = self.config.get(key, default)
        return value if isinstance(value, list) and value else default

    def _bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return bool(value)

    def _format_time(self, timestamp: Any) -> str:
        if not timestamp:
            return "未知"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))

    def _human_duration(self, seconds: int) -> str:
        if seconds % 86400 == 0 and seconds >= 86400:
            return f"{seconds // 86400}天"
        if seconds % 3600 == 0 and seconds >= 3600:
            return f"{seconds // 3600}小时"
        if seconds % 60 == 0 and seconds >= 60:
            return f"{seconds // 60}分钟"
        return f"{seconds}秒"

    async def terminate(self):
        self._save_stats()
