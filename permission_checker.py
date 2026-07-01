# -*- coding: utf-8 -*-
"""
命令权限检查中间件

提供统一的命令权限验证机制，实现：
- 自动应用Web UI配置的权限规则
- 实时生效的权限变更机制
- 权限判断逻辑与业务代码的解耦
"""
from typing import Optional, Tuple, Dict, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .permission_manager import PermissionLevel, command_registry


class PermissionChecker:
    """命令权限检查器
    
    职责：
    1. 根据命令名称和用户角色，判断用户是否有权限执行该命令
    2. 支持群级别的权限覆盖
    3. 提供实时的权限变更生效机制
    """
    
    def __init__(self, storage, onebot_mixin):
        """
        初始化权限检查器
        
        Args:
            storage: SQLiteStorage 实例，用于读取权限配置
            onebot_mixin: OneBotMixin 实例，用于获取用户角色信息
        """
        self._storage = storage
        self._onebot = onebot_mixin
        # 权限配置内存缓存：{command: {permission_level, enabled, allow_group_override}}
        self._permission_cache = {}
        # 群级别权限覆盖缓存：{group_id: {command: {permission_level, enabled}}}
        self._group_permission_cache = {}
        # 缓存过期时间（秒）
        self._cache_ttl = 30
        # 上次缓存刷新时间
        self._last_cache_refresh = 0
    
    async def _refresh_cache_if_needed(self) -> None:
        """如果缓存过期则刷新"""
        import time
        now = time.time()
        if now - self._last_cache_refresh > self._cache_ttl:
            await self._refresh_permission_cache()
            self._last_cache_refresh = now
    
    async def _refresh_permission_cache(self) -> None:
        """从数据库刷新权限配置缓存"""
        try:
            # 刷新全局权限配置
            permissions = self._storage.list_command_permissions()
            self._permission_cache = {}
            for perm in permissions:
                self._permission_cache[perm["command"]] = {
                    "permission_level": perm["permission_level"],
                    "enabled": perm["enabled"],
                    "allow_group_override": perm["allow_group_override"],
                }
            
            # 刷新群级别权限覆盖
            # 注意：这里只缓存已配置的群，避免加载所有群的配置
            # 实际使用时会按需加载
            self._group_permission_cache = {}
            
            logger.debug(f"[PermissionChecker] 权限缓存已刷新，共 {len(self._permission_cache)} 条配置")
        except Exception as e:
            logger.warning(f"[PermissionChecker] 刷新权限缓存失败: {e}")
    
    async def _get_group_permission_override(self, group_id: str, command: str) -> Optional[dict]:
        """获取群级别的命令权限覆盖"""
        # 先从缓存中查找
        if group_id in self._group_permission_cache:
            if command in self._group_permission_cache[group_id]:
                return self._group_permission_cache[group_id][command]
        
        # 缓存中没有，从数据库加载
        try:
            group_perm = self._storage.get_group_command_permission(group_id, command)
            if group_perm:
                # 更新缓存
                if group_id not in self._group_permission_cache:
                    self._group_permission_cache[group_id] = {}
                self._group_permission_cache[group_id][command] = {
                    "permission_level": group_perm["permission_level"],
                    "enabled": group_perm["enabled"],
                }
                return self._group_permission_cache[group_id][command]
        except Exception as e:
            logger.warning(f"[PermissionChecker] 获取群级别权限覆盖失败: {e}")
        
        return None
    
    async def _get_user_permission_level(self, event: AstrMessageEvent) -> Tuple[PermissionLevel, str]:
        """获取用户的权限级别
        
        Returns:
            Tuple[PermissionLevel, str]: (权限级别, 原因说明)
        """
        group_id = self._onebot._get_group_id(event)
        user_id = self._onebot._try_get_sender_id(event)
        
        if not group_id or not user_id:
            return PermissionLevel.ALL_MEMBERS, "无法获取用户信息"
        
        # 检查是否是插件管理员
        if await self._onebot._is_plugin_admin(event):
            return PermissionLevel.PLUGIN_ADMIN, "插件管理员"
        
        # 检查是否是群超管
        if self._storage.is_group_super_admin(group_id, user_id):
            return PermissionLevel.GROUP_SUPER_ADMIN, "群超管"
        
        # 获取群角色
        role = await self._onebot._get_member_role(event, group_id, user_id)
        
        if role == "owner":
            return PermissionLevel.GROUP_OWNER, "群主"
        elif role == "admin":
            return PermissionLevel.GROUP_ADMIN, "群管理员"
        else:
            return PermissionLevel.ALL_MEMBERS, "普通成员"
    
    async def check_permission(self, event: AstrMessageEvent, command: str) -> "PermissionResult":
        """检查用户是否有权限执行指定命令

        v2.4.4 起：如果命令未在数据库中配置权限级别，返回"请先在 WebUI 中设置权限"。
        权限级别完全由 WebUI 控制，代码中不再预设默认权限。

        Args:
            event: 消息事件
            command: 命令名称

        Returns:
            PermissionResult: 权限检查结果
        """
        await self._refresh_cache_if_needed()

        group_id = self._onebot._get_group_id(event)
        user_id = self._onebot._try_get_sender_id(event)

        # 插件管理员始终拥有最高权限，绕过所有权限配置检查
        # 这确保即使权限未配置，插件管理员也能正常管理
        user_level, level_desc = await self._get_user_permission_level(event)
        if user_level == PermissionLevel.PLUGIN_ADMIN:
            return PermissionResult(
                allowed=True,
                level=user_level,
                reason=f"权限检查通过（{level_desc}）"
            )

        # 获取命令的权限配置（从数据库缓存）
        perm_config = self._permission_cache.get(command)

        # 如果数据库中没有配置，尝试从注册表获取（注册表可能也没有默认权限）
        if not perm_config:
            registry_entry = command_registry.get(command)
            if registry_entry and registry_entry.permission_level is not None:
                # 注册表中有默认权限级别（向后兼容旧版本写入的配置）
                perm_config = {
                    "permission_level": registry_entry.permission_level.value,
                    "enabled": registry_entry.enabled,
                    "allow_group_override": registry_entry.allow_group_override,
                }
            else:
                # 命令未配置权限级别，提示需要在 WebUI 中设置
                return PermissionResult(
                    allowed=False,
                    level=user_level,
                    reason=f"命令「{command}」尚未配置权限级别，请在 WebUI 指令列表中设置所需权限"
                )

        # 检查权限级别是否为 -1（表示未配置，由 _sync_registry_to_db 写入的占位条目）
        if perm_config.get("permission_level") == -1 or perm_config.get("permission_level") is None:
            return PermissionResult(
                allowed=False,
                level=user_level,
                reason=f"命令「{command}」尚未配置权限级别，请在 WebUI 指令列表中设置所需权限"
            )
        
        # 检查命令是否启用
        if not perm_config["enabled"]:
            user_level, _ = await self._get_user_permission_level(event)
            # 即使命令禁用，插件管理员仍可使用
            if user_level == PermissionLevel.PLUGIN_ADMIN:
                return PermissionResult(
                    allowed=True,
                    level=user_level,
                    reason="命令已禁用，但您是插件管理员"
                )
            return PermissionResult(
                allowed=False,
                level=user_level,
                reason="命令已禁用"
            )
        
        # 检查是否有群级别覆盖
        required_level = perm_config["permission_level"]
        if group_id and perm_config["allow_group_override"]:
            group_override = await self._get_group_permission_override(group_id, command)
            if group_override:
                if not group_override["enabled"]:
                    user_level, _ = await self._get_user_permission_level(event)
                    if user_level == PermissionLevel.PLUGIN_ADMIN:
                        return PermissionResult(
                            allowed=True,
                            level=user_level,
                            reason="命令在本群已禁用，但您是插件管理员"
                        )
                    return PermissionResult(
                        allowed=False,
                        level=user_level,
                        reason="命令在本群已禁用"
                    )
                required_level = group_override["permission_level"]
        
        # 获取用户权限级别
        user_level, level_desc = await self._get_user_permission_level(event)
        
        # 权限级别比较（数值越小权限越高）
        if user_level.value <= required_level:
            return PermissionResult(
                allowed=True,
                level=user_level,
                reason=f"权限检查通过（{level_desc}）"
            )
        else:
            # 构建权限级别名称映射
            level_names = {
                PermissionLevel.PLUGIN_ADMIN: "插件管理员",
                PermissionLevel.GROUP_SUPER_ADMIN: "群超管",
                PermissionLevel.GROUP_OWNER: "群主",
                PermissionLevel.GROUP_ADMIN: "群管理员",
                PermissionLevel.ALL_MEMBERS: "所有成员",
            }
            required_name = level_names.get(PermissionLevel(required_level), f"级别{required_level}")
            
            return PermissionResult(
                allowed=False,
                level=user_level,
                reason=f"权限不足，需要 {required_name} 或更高权限，当前为 {level_desc}"
            )
    
    async def invalidate_cache(self, command: str = None, group_id: str = None) -> None:
        """使权限缓存失效
        
        Args:
            command: 指定命令，如果提供则只清除该命令的缓存
            group_id: 指定群号，如果提供则只清除该群的缓存
        """
        if command and group_id:
            # 清除特定群的特定命令缓存
            if group_id in self._group_permission_cache:
                self._group_permission_cache[group_id].pop(command, None)
        elif command:
            # 清除特定命令的全局缓存
            self._permission_cache.pop(command, None)
            # 同时清除所有群中该命令的缓存
            for gid in self._group_permission_cache:
                self._group_permission_cache[gid].pop(command, None)
        elif group_id:
            # 清除特定群的所有缓存
            self._group_permission_cache.pop(group_id, None)
        else:
            # 清除所有缓存
            self._permission_cache.clear()
            self._group_permission_cache.clear()
        
        # 强制下次检查时刷新缓存
        self._last_cache_refresh = 0
        
        logger.debug(f"[PermissionChecker] 权限缓存已失效")
    
    def register_command(self, command: str, description: str = "", category: str = "其他",
                        default_level: Optional[PermissionLevel] = None) -> None:
        """注册命令到权限系统

        v2.4.4 起不再自动将默认配置写入数据库。
        只注册到内存注册表（用于 WebUI 显示命令列表）。
        权限级别由用户在 WebUI 中手动设置后才会写入数据库。
        """
        command_registry.register(command, description, category, default_level)


class PermissionResult:
    """权限检查结果"""
    
    def __init__(self, allowed: bool, level: 'PermissionLevel', reason: str = ""):
        self.allowed = allowed
        self.level = level
        self.reason = reason
    
    @property
    def is_plugin_admin(self) -> bool:
        """是否是插件管理员"""
        return self.level == PermissionLevel.PLUGIN_ADMIN
    
    @property
    def is_group_super_admin(self) -> bool:
        """是否是群超管"""
        return self.level == PermissionLevel.GROUP_SUPER_ADMIN
    
    @property
    def is_group_owner(self) -> bool:
        """是否是群主"""
        return self.level == PermissionLevel.GROUP_OWNER
    
    @property
    def is_group_admin(self) -> bool:
        """是否是群管理员"""
        return self.level in (PermissionLevel.GROUP_OWNER, PermissionLevel.GROUP_ADMIN)
    
    def __str__(self) -> str:
        return f"PermissionResult(allowed={self.allowed}, level={self.level}, reason='{self.reason}')"