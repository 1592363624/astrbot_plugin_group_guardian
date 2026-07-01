# -*- coding: utf-8 -*-
"""
命令权限管理模块

提供基于Web UI的命令权限管理系统，包括：
- 命令权限配置数据模型
- 权限验证中间件
- 权限管理API接口
- 权限变更实时生效机制
"""
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional, Set


class PermissionLevel(IntEnum):
    """权限级别枚举，数值越小权限越高"""
    PLUGIN_ADMIN = 0      # 插件管理员（全局管理员）
    GROUP_SUPER_ADMIN = 1 # 群超管（特定群的专属管理员）
    GROUP_OWNER = 2       # 群主
    GROUP_ADMIN = 3       # 群管理员
    ALL_MEMBERS = 4       # 所有成员（包括普通成员）


@dataclass
class CommandPermission:
    """命令权限配置数据模型"""
    command: str                          # 命令名称
    permission_level: Optional[PermissionLevel] = None  # 所需最低权限级别，None 表示未配置（需在 WebUI 设置）
    enabled: bool = True                  # 命令是否启用
    description: str = ""                 # 命令描述
    category: str = "其他"                # 命令分类
    allow_group_override: bool = True     # 是否允许群级别覆盖
    created_at: int = 0                   # 创建时间
    updated_at: int = 0                   # 更新时间


@dataclass
class PermissionChangeLog:
    """权限变更日志数据模型"""
    id: int = 0
    command: str = ""                     # 命令名称
    operator: str = ""                    # 操作人
    old_level: Optional[int] = None       # 旧权限级别
    new_level: Optional[int] = None       # 新权限级别
    old_enabled: Optional[bool] = None    # 旧启用状态
    new_enabled: Optional[bool] = None    # 新启用状态
    change_type: str = ""                 # 变更类型：create/update/delete
    group_id: str = ""                    # 群级别覆盖时的群号
    timestamp: int = 0                    # 变更时间
    details: str = ""                     # 变更详情


@dataclass
class CommandRegistry:
    """命令注册表，自动发现和管理所有可配置的命令"""
    _commands: Dict[str, CommandPermission] = field(default_factory=dict)
    _categories: Dict[str, List[str]] = field(default_factory=dict)
    
    def register(self, command: str, description: str = "", category: str = "其他",
                 default_level: Optional[PermissionLevel] = None) -> None:
        """注册命令到注册表

        v2.4.4 起 default_level 默认为 None，表示未配置权限。
        权限级别完全由 WebUI 控制，代码中不再预设默认权限。
        """
        if command not in self._commands:
            now = int(time.time())
            self._commands[command] = CommandPermission(
                command=command,
                permission_level=default_level,
                description=description,
                category=category,
                created_at=now,
                updated_at=now
            )
        # 更新分类索引
        if category not in self._categories:
            self._categories[category] = []
        if command not in self._categories[category]:
            self._categories[category].append(command)
    
    def get(self, command: str) -> Optional[CommandPermission]:
        """获取命令权限配置"""
        return self._commands.get(command)
    
    def get_all(self) -> Dict[str, CommandPermission]:
        """获取所有命令权限配置"""
        return self._commands.copy()
    
    def get_categories(self) -> Dict[str, List[str]]:
        """获取所有分类及其命令列表"""
        return self._categories.copy()
    
    def update(self, command: str, permission: CommandPermission) -> None:
        """更新命令权限配置"""
        old = self._commands.get(command)
        if old:
            # 更新分类索引
            if old.category in self._categories:
                if command in self._categories[old.category]:
                    self._categories[old.category].remove(command)
            if permission.category not in self._categories:
                self._categories[permission.category] = []
            if command not in self._categories[permission.category]:
                self._categories[permission.category].append(command)
        self._commands[command] = permission
    
    def remove(self, command: str) -> bool:
        """移除命令权限配置"""
        if command in self._commands:
            perm = self._commands.pop(command)
            if perm.category in self._categories:
                if command in self._categories[perm.category]:
                    self._categories[perm.category].remove(command)
            return True
        return False


# 全局命令注册表实例
command_registry = CommandRegistry()


# 命令分类常量
CATEGORY_MODERATION = "审核管理"
CATEGORY_GROUP_MANAGEMENT = "群管理"
CATEGORY_MEMBER_MANAGEMENT = "成员管理"
CATEGORY_INFO_QUERY = "信息查询"
CATEGORY_BATCH_OPERATIONS = "批量操作"
CATEGORY_PLUGIN_CONFIG = "插件配置"
CATEGORY_OTHER = "其他"


def register_command(command: str, description: str = "", category: str = CATEGORY_OTHER,
                     default_level: Optional[PermissionLevel] = None) -> None:
    """便捷的命令注册装饰器/函数

    v2.4.4 起 default_level 默认为 None，权限级别完全由 WebUI 控制。
    """
    command_registry.register(command, description, category, default_level)


def auto_discover_commands() -> None:
    """自动发现并注册所有已知命令

    v2.4.4 起只注册命令名、描述和分类（用于 WebUI 显示），不再预设默认权限级别。
    新命令在 WebUI 中显示为"未配置"，使用时提示需要在 WebUI 中设置权限。
    """
    # 审核管理类
    register_command("自动审核", "开关智能审核功能", CATEGORY_MODERATION)

    # 群管理类
    register_command("全体禁言", "开启或关闭全员禁言", CATEGORY_GROUP_MANAGEMENT)
    register_command("群名", "修改群聊名称", CATEGORY_GROUP_MANAGEMENT)
    register_command("发公告", "发布群公告", CATEGORY_GROUP_MANAGEMENT)
    register_command("删公告", "删除群公告", CATEGORY_GROUP_MANAGEMENT)
    register_command("加群方式", "修改入群验证方式", CATEGORY_GROUP_MANAGEMENT)
    register_command("群管理授权", "群管理员授权开关", CATEGORY_GROUP_MANAGEMENT)

    # 成员管理类
    register_command("禁言", "禁言指定群成员", CATEGORY_MEMBER_MANAGEMENT)
    register_command("解禁", "解除指定群成员禁言", CATEGORY_MEMBER_MANAGEMENT)
    register_command("踢人", "将成员移出群聊", CATEGORY_MEMBER_MANAGEMENT)
    register_command("设置名片", "修改成员群名片", CATEGORY_MEMBER_MANAGEMENT)
    register_command("头衔", "设置成员专属头衔", CATEGORY_MEMBER_MANAGEMENT)
    register_command("设置管理", "设置或取消群管理员", CATEGORY_MEMBER_MANAGEMENT)
    register_command("设置管理插件", "管理插件管理员列表", CATEGORY_PLUGIN_CONFIG)

    # 信息查询类
    register_command("字数统计", "统计群内关键词出现次数", CATEGORY_INFO_QUERY)
    register_command("群统计", "显示群内今日消息统计", CATEGORY_INFO_QUERY)
    register_command("搜索成员", "按昵称或QQ号搜索群成员", CATEGORY_INFO_QUERY)
    register_command("成员列表", "查看群成员列表", CATEGORY_INFO_QUERY)
    register_command("禁言列表", "查看当前被禁言的成员", CATEGORY_INFO_QUERY)
    register_command("公告列表", "查看群公告列表", CATEGORY_INFO_QUERY)
    register_command("文件列表", "查看群文件列表", CATEGORY_INFO_QUERY)

    # 批量操作类
    register_command("批量撤回", "批量撤回最近消息", CATEGORY_BATCH_OPERATIONS)
    register_command("批量禁言", "批量禁言多人", CATEGORY_BATCH_OPERATIONS)
    register_command("批量踢人", "批量踢出多人", CATEGORY_BATCH_OPERATIONS)

    # 消息操作类
    register_command("撤回最新消息", "撤回群内最新一条或多条消息", CATEGORY_GROUP_MANAGEMENT)
    register_command("设精华", "设置精华消息", CATEGORY_GROUP_MANAGEMENT)
    register_command("取消精华", "取消精华消息", CATEGORY_GROUP_MANAGEMENT)
    register_command("删文件", "删除群文件", CATEGORY_GROUP_MANAGEMENT)

    # 权限管理类
    register_command("移除群管权限", "群主移除本群某群管的bot管理权限", CATEGORY_PLUGIN_CONFIG)
    register_command("恢复群管权限", "群主恢复本群某群管的bot管理权限", CATEGORY_PLUGIN_CONFIG)
