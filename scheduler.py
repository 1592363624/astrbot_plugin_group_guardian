# -*- coding: utf-8 -*-
"""后台调度模块（v2.5.0）。

负责两类周期性任务 + 一类事件驱动任务：
1. F3 定时自动解禁：扫描 scheduled_unbans 表，到期则解禁并删除记录。
2. F2 申诉超时：把过期仍 waiting 的申诉标记 expired（维持原处罚）。
3. 进群确认公告（事件驱动）：用户进群时启动检查任务，全部确认后自动停止。

设计要点：
- 主调度器按 auto_unban_scan_interval 秒轮询，仅处理定时解禁和申诉超时；
- 进群确认检查为独立 asyncio 任务，事件驱动按需启停，间隔 join_ban_confirm_check_interval 秒；
- 后台任务无 event，OneBot client 通过 _get_client(None) 的 platform_manager 回退获取；
- 插件 terminate 时取消所有任务，避免热重载后残留协程。
"""
import asyncio
import time

from astrbot.api import logger


class SchedulerMixin:
    def _init_scheduler(self) -> None:
        """初始化调度器状态。在 __init__ 中调用。"""
        self._scheduler_task = None
        self._scheduler_stop = False
        # 进群确认检查任务（事件驱动：有人进群时启动，全部确认后停止）
        self._join_check_task = None

    def _start_scheduler(self) -> None:
        """启动后台调度 loop（幂等：已在运行则不重复启动）。"""
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_stop = False
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        except RuntimeError:
            # 无运行中的事件循环（极少见），放弃后台任务，不影响其它功能
            logger.debug("[GroupMgr] 无事件循环，跳过调度器启动")

    async def _stop_scheduler(self) -> None:
        """停止后台调度 loop。在 terminate 中调用。"""
        self._scheduler_stop = True
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                logger.debug("[GroupMgr] 调度器任务已取消")
        # 同时停止进群确认检查任务
        if self._join_check_task and not self._join_check_task.done():
            self._join_check_task.cancel()
            try:
                await self._join_check_task
            except asyncio.CancelledError:
                logger.debug("[GroupMgr] 进群确认检查任务已取消")

    def _start_join_confirm_check(self) -> None:
        """启动进群确认检查任务（幂等：已在运行则不重复启动）。

        由 membership.py 的 _handle_group_increase 在用户进群时调用。
        任务在 _join_confirm_check_loop 中运行，无 pending 记录时自动退出。
        """
        if self._join_check_task and not self._join_check_task.done():
            return
        try:
            self._join_check_task = asyncio.create_task(self._join_confirm_check_loop())
            logger.info("[GroupMgr] 进群确认检查任务已启动")
        except RuntimeError:
            logger.debug("[GroupMgr] 无事件循环，跳过进群确认检查任务启动")

    async def _scheduler_loop(self) -> None:
        consecutive_errors = 0
        while not self._scheduler_stop:
            interval = self._clamp_int(self.config.get("auto_unban_scan_interval", 60), 60, 10, 3600)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(60)
            if self._scheduler_stop:
                break
            try:
                await self._run_due_unbans()
                expire_fn = getattr(self, "_expire_appeals", None)
                if expire_fn:
                    await expire_fn()
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[GroupMgr] 调度任务出错({consecutive_errors}): {e}")
                if consecutive_errors >= 10:
                    logger.error("[GroupMgr] 调度器连续错误过多，暂停 5 分钟")
                    await asyncio.sleep(300)
                    consecutive_errors = 0

    async def _run_due_unbans(self) -> None:
        """执行所有到期的定时解禁。"""
        now = int(time.time())
        due = self._storage.list_due_unbans(now)
        if not due:
            return
        for item in due:
            gid = str(item.get("group_id", ""))
            uid = item.get("user_id", "")
            if not self._cfg("auto_unban_enabled", False, group_id=gid):
                continue
            ok = await self._unban_member(gid, uid)
            # 不论 API 成功与否都删除记录，避免失败项反复重试堆积；失败已在 _unban_member 记日志
            self._storage.delete_scheduled_unban(item.get("id"))
            if ok:
                logger.info(f"[GroupMgr] 定时解禁: 群{gid} 用户{uid}")

    def _schedule_unban(self, group_id: str, user_id: str, mute_seconds: int) -> None:
        """登记一条定时解禁计划。mute_seconds<=0（永久禁言）时按一个较大的兜底时长处理。

        仅在 auto_unban_enabled 开启时登记。OneBot 自带到期解禁，本功能用于
        永久禁言托管解禁、重启后补解禁等场景。
        """
        if not group_id or not user_id:
            return
        group_id = str(group_id)
        if not self._cfg("auto_unban_enabled", False, group_id=group_id):
            return
        now = int(time.time())
        if mute_seconds and mute_seconds > 0:
            unban_at = now + int(mute_seconds)
        else:
            # 永久禁言：按配置的兜底托管时长（默认 7 天）后解禁
            fallback = self._cfg_int("auto_unban_permanent_hours", 168, group_id=group_id)
            unban_at = now + fallback * 3600
        try:
            self._storage.add_scheduled_unban(group_id, user_id, unban_at, now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 登记定时解禁失败: {e}")

    async def _join_confirm_check_loop(self) -> None:
        """进群确认检查循环（事件驱动按需运行）。

        由 _start_join_confirm_check 启动。每轮：
        1. 清理超时未确认的 pending 记录（标记为 timeout）
        2. 查询剩余 pending 记录，无记录则退出
        3. 按群分组检查群公告阅读数，阅读数增加则解禁用户
        """
        consecutive_errors = 0
        while True:
            interval = self._clamp_int(
                self.config.get("join_ban_confirm_check_interval", 15), 15, 5, 3600
            )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(15)

            try:
                # 1. 超时清理：将超过阈值的 pending 记录标记为 timeout
                timeout_secs = self._clamp_int(
                    self.config.get("join_ban_confirm_check_timeout", 86400), 86400, 3600, 604800
                )
                timed_out = self._storage.timeout_pending_confirmations(timeout_secs)
                if timed_out > 0:
                    logger.info(f"[GroupMgr] 进群确认: {timed_out} 条记录超时未确认，已标记 timeout")

                # 2. 查询剩余 pending 记录
                pending = self._storage.list_pending_confirmations()
                if not pending:
                    logger.info("[GroupMgr] 进群确认检查任务：无待确认记录，任务退出")
                    break

                # 3. 按群分组检查阅读数（带缓存，同一群只查一次）
                group_read_nums = {}  # group_id -> 当前阅读数

                for record in pending:
                    group_id = record.get("group_id", "")
                    user_id = record.get("user_id", "")
                    initial_read_num = record.get("initial_read_num", 0)
                    nickname = record.get("nickname", "") or user_id

                    if not group_id or not user_id:
                        continue

                    # 检查该群是否启用进群确认功能
                    if not self._cfg("join_ban_confirm_enabled", False, group_id=group_id):
                        continue

                    # 获取当前群公告阅读数（带缓存，同一群只查一次）
                    if group_id not in group_read_nums:
                        current_read_num = await self._get_group_notice_read_num(group_id)
                        group_read_nums[group_id] = current_read_num
                    else:
                        current_read_num = group_read_nums[group_id]

                    # 如果阅读数增加了，说明用户已确认公告
                    if current_read_num > initial_read_num:
                        logger.info(f"[GroupMgr] 进群确认: 检测到群公告阅读数增加（{initial_read_num} -> {current_read_num}），解禁用户 {nickname}({user_id})")

                        # 解除禁言
                        unban_ok = await self._unban_member(group_id, user_id)
                        if unban_ok:
                            # 更新确认记录
                            self._storage.confirm_join_user(group_id, user_id)
                            logger.info(f"[GroupMgr] 进群确认: 已解禁用户 {nickname}({user_id})")

                            # 发送解禁成功通知
                            try:
                                client = await self._get_client()
                                if client:
                                    gid_int = self._safe_int(group_id, 0)
                                    if gid_int:
                                        await client.call_action("send_group_msg", group_id=gid_int,
                                                               message=f"[进群确认] {nickname}({user_id}) 已确认群公告，禁言已解除")
                            except Exception as e:
                                logger.debug(f"[GroupMgr] 进群确认: 发送解禁通知失败 {e}")
                        else:
                            logger.warning(f"[GroupMgr] 进群确认: 解禁失败 群={group_id} 用户={user_id}")

                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[GroupMgr] 进群确认检查任务出错({consecutive_errors}): {e}")
                if consecutive_errors >= 10:
                    logger.error("[GroupMgr] 进群确认检查任务连续错误过多，任务退出")
                    break
