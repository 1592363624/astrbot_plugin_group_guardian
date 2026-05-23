# -*- coding: utf-8 -*-
import asyncio
import inspect
from collections import deque
from typing import Dict, Tuple

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .commands import CommandsMixin
from .constants import PLUGIN_NAME, PLUGIN_VERSION
from .llm_tools import LlmToolsMixin
from .moderation import ModerationMixin
from .onebot import OneBotMixin
from .patterns import AD_PATTERNS, SWEAR_PATTERNS
from .storage import SQLiteStorage
from .utils import UtilitiesMixin
from .web import WebMixin


class Main(ModerationMixin, LlmToolsMixin, WebMixin, OneBotMixin, UtilitiesMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._config_schema = self._load_config_schema()
        self._sync_astrbot_admins()
        self._client = None
        self._data_dir = StarTools.get_data_dir()
        self._storage = SQLiteStorage(self._data_dir, self._get_plugin_dir())
        self._storage.initialize()
        _gwl = self.config.get("group_white_list", [])
        self.group_white_list = [str(g).strip() for g in (_gwl if isinstance(_gwl, list) else [_gwl]) if g]
        self._group_white_set = set(self.group_white_list)
        _gbl = self.config.get("group_black_list", [])
        self.group_black_list = [str(g).strip() for g in (_gbl if isinstance(_gbl, list) else [_gbl]) if g]
        self._group_black_set = set(self.group_black_list)
        _ubl = self.config.get("user_black_list", [])
        self.user_black_list = [str(u).strip() for u in (_ubl if isinstance(_ubl, list) else [_ubl]) if u]
        self._user_black_set = set(self.user_black_list)
        self.auto_moderate_enabled = self.config.get("auto_moderate_enabled", True)
        self._compiled_swear = self._build_combined_regex(SWEAR_PATTERNS)
        self._compiled_ad = self._build_combined_regex(AD_PATTERNS)
        self._lexicon = self._load_lexicon()
        self._compiled_lexicon = self._compile_lexicon()
        self._moderation_logs = deque(self._load_logs(), maxlen=500)
        self._next_log_id = max(self._init_next_log_id(), self._storage.max_log_id() + 1)
        self._last_log_save = 0.0
        self._log_save_task = None
        self._admin_role_cache: Dict[str, Tuple[bool, float]] = {}
        self._admin_role_cache_ttl = 300.0
        self._stats_cache = {"today_start": 0, "blocked": 0, "passed": 0, "total": 0, "group_stats": {}, "user_stats": {}}
        self._llm_semaphore = asyncio.Semaphore(5)
        self._register_web_apis()

    async def terminate(self):
        logger.info("[GroupMgr] 插件卸载，SQLite 存储已自动持久化")


_COMMANDS = {
    "字数统计": "word_count",
    "群统计": "group_stats",
    "搜索成员": "search_member",
    "撤回最新消息": "recall_last",
    "禁言": "cmd_ban",
    "解禁": "cmd_unban",
    "踢人": "cmd_kick",
    "全体禁言": "cmd_whole_ban",
    "设置名片": "cmd_set_card",
    "发公告": "cmd_send_notice",
    "删公告": "cmd_delete_notice",
    "公告列表": "cmd_list_notices",
    "文件列表": "cmd_list_files",
    "删文件": "cmd_delete_file",
    "成员列表": "cmd_member_list",
    "禁言列表": "cmd_banned_list",
    "群名": "cmd_set_name",
    "头衔": "cmd_set_title",
    "设精华": "cmd_set_essence",
    "取消精华": "cmd_del_essence",
    "设置管理": "cmd_set_admin",
    "加群方式": "cmd_join_verify",
    "自动审核": "cmd_auto_moderate",
    "设置管理插件": "cmd_plugin_admin",
    "批量撤回": "recall_all",
}
_ADMIN_COMMAND_METHODS = {
    "search_member",
    "recall_last",
    "cmd_ban",
    "cmd_unban",
    "cmd_kick",
    "cmd_whole_ban",
    "cmd_set_card",
    "cmd_send_notice",
    "cmd_delete_notice",
    "cmd_delete_file",
    "cmd_set_name",
    "cmd_set_title",
    "cmd_set_essence",
    "cmd_del_essence",
    "cmd_set_admin",
    "cmd_join_verify",
    "cmd_auto_moderate",
    "cmd_plugin_admin",
    "recall_all",
}
_LLM_TOOLS = {
    "ban_group_member": "ban_group_member_tool",
    "unban_group_member": "unban_group_member_tool",
    "kick_group_member": "kick_group_member_tool",
    "set_whole_group_ban": "set_whole_group_ban_tool",
    "set_member_card": "set_member_card_tool",
    "send_group_announcement": "send_group_announcement_tool",
    "get_group_member_list": "get_group_member_list_tool",
    "set_group_admin": "set_group_admin_tool",
    "set_group_name": "set_group_name_tool",
    "set_member_title": "set_member_title_tool",
    "get_banned_members": "get_banned_members_tool",
    "set_group_join_verify": "set_group_join_verify_tool",
    "recall_message": "recall_message_tool",
    "set_essence_message": "set_essence_message_tool",
    "delete_essence_message": "delete_essence_message_tool",
    "delete_group_notice": "delete_group_notice_tool",
    "list_group_files": "list_group_files_tool",
    "delete_group_file": "delete_group_file_tool",
    "get_group_notice_list": "get_group_notice_list_tool",
    "upload_group_file": "upload_group_file_tool",
}


def _strip_decorators(func):
    for attr in ("__decorated__", "__decorated_event__", "__decorated_platform__"):
        if hasattr(func, attr):
            delattr(func, attr)


def _rebind_handler(func, name):
    if inspect.isasyncgenfunction(func):
        async def wrapper(self, *args, **kwargs):
            async for item in func(self, *args, **kwargs):
                yield item
    else:
        async def wrapper(self, *args, **kwargs):
            return await func(self, *args, **kwargs)
    wrapper.__name__ = name
    wrapper.__doc__ = getattr(func, "__doc__", None)
    wrapper.__annotations__ = dict(getattr(func, "__annotations__", {}))
    wrapper.__signature__ = inspect.signature(func)
    wrapper.__module__ = PLUGIN_NAME
    wrapper.__qualname__ = f"Main.{name}"
    return wrapper


for _command_name, _method_name in _COMMANDS.items():
    _source = getattr(CommandsMixin, _method_name)
    _strip_decorators(_source)
    _handler = _rebind_handler(_source, _method_name)
    if _method_name in _ADMIN_COMMAND_METHODS:
        _handler = filter.permission_type(filter.PermissionType.ADMIN)(_handler)
    _handler = filter.command(_command_name)(_handler)
    _handler.__module__ = PLUGIN_NAME
    _handler.__qualname__ = f"Main.{_method_name}"
    setattr(Main, _method_name, _handler)

for _tool_name, _method_name in _LLM_TOOLS.items():
    _source = getattr(LlmToolsMixin, _method_name)
    _strip_decorators(_source)
    _handler = _rebind_handler(_source, _method_name)
    _handler = filter.llm_tool(name=_tool_name)(_handler)
    _handler.__module__ = PLUGIN_NAME
    _handler.__qualname__ = f"Main.{_method_name}"
    setattr(Main, _method_name, _handler)

_handle_message_source = ModerationMixin._handle_message
_strip_decorators(_handle_message_source)
_handle_message = _rebind_handler(_handle_message_source, "_handle_message")
_handle_message = filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)(_handle_message)
_handle_message = filter.event_message_type(filter.EventMessageType.ALL)(_handle_message)
_handle_message.__module__ = PLUGIN_NAME
_handle_message.__qualname__ = "Main._handle_message"
setattr(Main, "_handle_message", _handle_message)
setattr(Main, "_search_keyword_in_messages", CommandsMixin._search_keyword_in_messages)


Main = register(
    PLUGIN_NAME,
    "zhaisir",
    "QQ群智能守护者 - AI审核+群管工具集",
    PLUGIN_VERSION,
    "https://github.com/zcj-ui/astrbot_plugin_group_guardian",
)(Main)
