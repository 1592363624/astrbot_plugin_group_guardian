# -*- coding: utf-8 -*-
import time
from collections import deque
from typing import Dict, Optional, Tuple


class AntiFloodMixin:
    """防刷屏检测模块。按 (群号, 用户ID) 追踪消息时间戳，超限自动禁言+可选撤回。

    特性：
    - 所有消息类型均计入（文本/图片/转发/QQ收藏/JSON/App 等）
    - 三档独立速率检测：每秒 / 每分钟 / 每小时
    - 每档可独立关闭：配置项设为 0 即跳过该检测
    - 管理员完全豁免（在 moderation.py 管线中提前 return）
    - 内存自动回收：每组每条用户队列上限 200 条，每 5 分钟清理超过 2 小时的过期数据
    """

    def _init_anti_flood(self):
        """在 Main.__init__ 中调用，初始化防刷屏数据结构。

        _anti_flood_data 结构:
          {group_id: {user_id: deque[(timestamp_float, message_id_str), ...]}}
        """
        self._anti_flood_data: Dict[str, Dict[str, deque]] = {}
        self._anti_flood_last_cleanup = 0.0

    def _record_message(self, group_id: str, user_id: str, msg_id: str):
        """记录一条消息到对应的群/用户时间戳队列中。

        Args:
            group_id: 群号字符串
            user_id: 发送者 QQ 号字符串
            msg_id: 消息 ID 字符串（用于撤回）
        """
        if group_id not in self._anti_flood_data:
            self._anti_flood_data[group_id] = {}
        if user_id not in self._anti_flood_data[group_id]:
            self._anti_flood_data[group_id][user_id] = deque(maxlen=200)
        self._anti_flood_data[group_id][user_id].append((time.time(), msg_id))

    def _get_rate_limit(self, key: str, default: int) -> int:
        """读取防刷屏速率配置。返回值 <= 0 表示该检测档位被关闭。"""
        return self._safe_int(self.config.get(key, default), default)

    def _check_anti_flood(self, group_id: str, user_id: str) -> Tuple[bool, Optional[dict]]:
        """检查用户是否触发刷屏阈值。按 每秒→每分钟→每小时 顺序检测，
        任一档位触发即返回结果，不再检测后续档位。

        Args:
            group_id: 群号
            user_id: 用户 QQ 号

        Returns:
            (False, None) — 未触发任何阈值
            (True, dict)  — 已触发，dict 包含:
                rate:       "每秒" / "每分钟" / "每小时"
                count:      该时间窗口内的消息数
                limit:      配置的阈值
                total_msgs: 用户当前在队列中的总消息数
                msg_ids:    该时间窗口内的消息 ID 列表（用于撤回）
        """
        if group_id not in self._anti_flood_data or user_id not in self._anti_flood_data[group_id]:
            return False, None

        entries = list(self._anti_flood_data[group_id][user_id])
        total_msgs = len(entries)
        now = time.time()

        # 每秒检测：limit=0 表示关闭此档位
        sec_limit = self._get_rate_limit("anti_flood_rate_per_second", 5)
        if sec_limit > 0:
            sec_entries = [(t, mid) for t, mid in entries if t > now - 1]
            if len(sec_entries) > sec_limit:
                return True, {"rate": "每秒", "count": len(sec_entries), "limit": sec_limit,
                              "total_msgs": total_msgs, "msg_ids": [mid for _, mid in sec_entries]}

        # 每分钟检测：limit=0 表示关闭此档位
        min_limit = self._get_rate_limit("anti_flood_rate_per_minute", 20)
        if min_limit > 0:
            min_entries = [(t, mid) for t, mid in entries if t > now - 60]
            if len(min_entries) > min_limit:
                return True, {"rate": "每分钟", "count": len(min_entries), "limit": min_limit,
                              "total_msgs": total_msgs, "msg_ids": [mid for _, mid in min_entries]}

        # 每小时检测：limit=0 表示关闭此档位
        hour_limit = self._get_rate_limit("anti_flood_rate_per_hour", 60)
        if hour_limit > 0:
            hour_entries = [(t, mid) for t, mid in entries if t > now - 3600]
            if len(hour_entries) > hour_limit:
                return True, {"rate": "每小时", "count": len(hour_entries), "limit": hour_limit,
                              "total_msgs": total_msgs, "msg_ids": [mid for _, mid in hour_entries]}

        return False, None

    def _anti_flood_cleanup(self):
        """每 5 分钟清理一次过期数据（超过 2 小时未被写入的队列），
        同时移除空队列和空群条目，防止内存无限增长。
        """
        now = time.time()
        if now - self._anti_flood_last_cleanup < 300:
            return
        self._anti_flood_last_cleanup = now
        expired = now - 7200
        for gid, users in list(self._anti_flood_data.items()):
            for uid in list(users.keys()):
                dq = users[uid]
                while dq and dq[0][0] < expired:
                    dq.popleft()
                if not dq:
                    del users[uid]
            if not users:
                del self._anti_flood_data[gid]

    def _get_anti_flood_status(self) -> dict:
        """返回当前防刷屏状态的快照，供 WebUI API 使用。
        数据经过脱敏（时间戳转成最近 N 秒的计数），方便前端渲染。

        Returns:
            {
                "enabled": bool,          # 是否已启用
                "tracked_groups": int,    # 正在追踪的群数量
                "tracked_users": int,     # 正在追踪的用户总数
                "groups": {               # 按群聚合的数据
                    "group_id": {
                        "users": {
                            "user_id": {
                                "total_msgs": int,       # 队列中总消息数
                                "per_second": int,       # 最近1秒的消息数
                                "per_minute": int,       # 最近60秒的消息数
                                "per_hour": int,         # 最近1小时的消息数
                            }
                        }
                    }
                }
            }
        """
        result = {
            "enabled": self._cfg("anti_flood_enabled", True),
            "tracked_groups": 0,
            "tracked_users": 0,
            "groups": {},
        }
        if not self._anti_flood_data:
            return result
        now = time.time()
        total_users = 0
        for gid, users in self._anti_flood_data.items():
            group_users = {}
            for uid, dq in users.items():
                entries = list(dq)
                if not entries:
                    continue
                total_users += 1
                group_users[uid] = {
                    "total_msgs": len(entries),
                    "per_second": sum(1 for t, _ in entries if t > now - 1),
                    "per_minute": sum(1 for t, _ in entries if t > now - 60),
                    "per_hour": sum(1 for t, _ in entries if t > now - 3600),
                }
            if group_users:
                result["groups"][gid] = {"users": group_users}
        result["tracked_groups"] = len(result["groups"])
        result["tracked_users"] = total_users
        return result
