# 进群确认检查事件驱动优化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将进群确认检查从主调度器轮询改为事件驱动按需独立任务，消除空闲轮询，缩短检查间隔至 15 秒，并增加超时清理机制。

**Architecture:** 从 `_scheduler_loop` 中移除 `_check_join_confirmations_by_read_num()` 调用，改为独立 asyncio 任务 `_join_confirm_check_loop()`。用户进群时启动任务，全部 pending 记录处理完成后任务自动退出。新增 `timeout` 状态和 `timeout_pending_confirmations()` 方法处理超时记录。

**Tech Stack:** Python asyncio, SQLite (storage), AstrBot 插件框架

## Global Constraints

- 代码注释使用中文
- 配置项抽取到 `_conf_schema.json`，不硬编码
- 保持现有 `_run_due_unbans` 和 `_expire_appeals` 逻辑不变
- 不修改 `list_pending_confirmations()`、`confirm_join_user()`、`save_join_confirmation()` 的签名

---

### Task 1: 存储层新增超时清理方法 + 配置项

**Files:**
- Modify: `storage.py:1867` (在 `delete_join_confirmation` 方法前插入新方法)
- Modify: `_conf_schema.json:611-616` (修改 `join_ban_confirm_check_interval`，新增 `join_ban_confirm_check_timeout`)

**Interfaces:**
- Produces: `storage.timeout_pending_confirmations(timeout_seconds: int) -> int` — 供 Task 2 的检查循环调用

**Steps:**

- [ ] **Step 1: 在 storage.py 中新增 `timeout_pending_confirmations` 方法**

在 `storage.py` 的 `delete_join_confirmation` 方法（第 1867 行）之前插入：

```python
    def timeout_pending_confirmations(self, timeout_seconds: int) -> int:
        """将超过 timeout_seconds 未确认的 pending 记录标记为 timeout。

        用于事件驱动检查任务的超时清理：用户进群后长时间未确认公告时，
        将其记录从 pending 改为 timeout，检查任务不再处理该记录，
        用户保持禁言状态由管理员手动处理。

        Args:
            timeout_seconds: 超时阈值（秒），从 banned_at 起计算

        Returns:
            被标记为 timeout 的记录数
        """
        now = int(time.time())
        cutoff = now - timeout_seconds
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE join_confirmations SET status='timeout' "
                "WHERE status='pending' AND banned_at < ?",
                (cutoff,)
            )
            conn.commit()
        return cur.rowcount
```

- [ ] **Step 2: 修改 `_conf_schema.json` 中的 `join_ban_confirm_check_interval`**

将 `_conf_schema.json` 第 611-616 行：

```json
    "join_ban_confirm_check_interval": {
        "description": "进群确认检查间隔（秒）",
        "type": "int",
        "default": 60,
        "hint": "定时检查群公告阅读数变化的间隔，默认60秒。阅读数增加则自动解禁用户"
    }
}
```

替换为：

```json
    "join_ban_confirm_check_interval": {
        "description": "进群确认检查间隔（秒）",
        "type": "int",
        "default": 15,
        "hint": "事件驱动检查任务的轮询间隔，默认15秒。有人进群时才启动检查，全部确认后自动停止"
    },
    "join_ban_confirm_check_timeout": {
        "description": "进群确认检查超时（秒）",
        "type": "int",
        "default": 86400,
        "hint": "用户进群后超过此时长未确认公告，则停止检查该用户（保持禁言），默认86400秒（24小时）"
    }
}
```

- [ ] **Step 3: 提交**

```bash
git add storage.py _conf_schema.json
git commit -m "feat: 新增进群确认超时清理方法与配置项"
```

---

### Task 2: 调度器核心改造——独立按需检查任务

**Files:**
- Modify: `scheduler.py:21-24` (`_init_scheduler` 新增 `_join_check_task` 初始化)
- Modify: `scheduler.py:37-45` (`_stop_scheduler` 新增 cancel `_join_check_task`)
- Modify: `scheduler.py:47-75` (`_scheduler_loop` 移除进群确认检查调用)
- Modify: `scheduler.py:117-174` (将 `_check_join_confirmations_by_read_num` 改造为 `_join_confirm_check_loop`，新增 `_start_join_confirm_check`)

**Interfaces:**
- Consumes: `storage.timeout_pending_confirmations(timeout_seconds: int) -> int` (Task 1)
- Consumes: `storage.list_pending_confirmations() -> List[dict]` (已有)
- Consumes: `storage.confirm_join_user(group_id: str, user_id: str) -> bool` (已有)
- Produces: `_start_join_confirm_check()` — 供 Task 3 在用户进群时调用

**Steps:**

- [ ] **Step 1: 在 `_init_scheduler` 中初始化 `_join_check_task`**

将 `scheduler.py` 第 21-24 行：

```python
    def _init_scheduler(self) -> None:
        """初始化调度器状态。在 __init__ 中调用。"""
        self._scheduler_task = None
        self._scheduler_stop = False
```

替换为：

```python
    def _init_scheduler(self) -> None:
        """初始化调度器状态。在 __init__ 中调用。"""
        self._scheduler_task = None
        self._scheduler_stop = False
        # 进群确认检查任务（事件驱动：有人进群时启动，全部确认后停止）
        self._join_check_task = None
```

- [ ] **Step 2: 在 `_stop_scheduler` 中 cancel `_join_check_task`**

将 `scheduler.py` 第 37-45 行：

```python
    async def _stop_scheduler(self) -> None:
        """停止后台调度 loop。在 terminate 中调用。"""
        self._scheduler_stop = True
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                logger.debug("[GroupMgr] 调度器任务已取消")
```

替换为：

```python
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
```

- [ ] **Step 3: 从 `_scheduler_loop` 中移除进群确认检查调用**

将 `scheduler.py` 第 59-66 行：

```python
            try:
                await self._run_due_unbans()
                expire_fn = getattr(self, "_expire_appeals", None)
                if expire_fn:
                    await expire_fn()
                # 检查进群确认公告：通过群公告阅读数变化自动解禁
                await self._check_join_confirmations_by_read_num()
                consecutive_errors = 0
```

替换为（删除第 64-65 行的进群确认调用）：

```python
            try:
                await self._run_due_unbans()
                expire_fn = getattr(self, "_expire_appeals", None)
                if expire_fn:
                    await expire_fn()
                consecutive_errors = 0
```

- [ ] **Step 4: 新增 `_start_join_confirm_check` 方法**

在 `_stop_scheduler` 方法之后（第 46 行之后）插入：

```python
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
```

- [ ] **Step 5: 将 `_check_join_confirmations_by_read_num` 改造为 `_join_confirm_check_loop`**

将 `scheduler.py` 第 117-174 行的整个 `_check_join_confirmations_by_read_num` 方法替换为：

```python
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
```

- [ ] **Step 6: 更新文件头部注释**

将 `scheduler.py` 第 1-13 行的文件注释：

```python
# -*- coding: utf-8 -*-
"""后台调度模块（v2.5.0）。

负责三类周期性任务：
1. F3 定时自动解禁：扫描 scheduled_unbans 表，到期则解禁并删除记录。
2. F2 申诉超时：把过期仍 waiting 的申诉标记 expired（维持原处罚）。
3. 进群确认公告：定期检查群公告阅读数变化，自动解禁已确认用户。

设计要点：
- 单个 asyncio 后台 loop，按 auto_unban_scan_interval 秒轮询；
- 后台任务无 event，OneBot client 通过 _get_client(None) 的 platform_manager 回退获取；
- 插件 terminate 时取消任务，避免热重载后残留协程。
"""
```

替换为：

```python
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
```

- [ ] **Step 7: 提交**

```bash
git add scheduler.py
git commit -m "feat: 进群确认检查改为事件驱动按需任务"
```

---

### Task 3: 接入触发点——进群事件启动 + 热重载兜底

**Files:**
- Modify: `membership.py:477-480` (`_handle_group_increase` 末尾新增启动调用)
- Modify: `main.py:102-103` (`__init__` 中新增遗留 pending 记录兜底检查)

**Interfaces:**
- Consumes: `_start_join_confirm_check()` (Task 2)
- Consumes: `storage.list_pending_confirmations() -> List[dict]` (已有)

**Steps:**

- [ ] **Step 1: 在 `_handle_group_increase` 末尾添加启动调用**

将 `membership.py` 第 476-480 行：

```python
        # 保存确认记录，包含初始阅读数
        self._storage.save_join_confirmation(group_id, user_id, nickname, msg_id, initial_read_num)
        logger.info(f"[GroupMgr] 进群确认: 已保存确认记录 群={group_id} 用户={user_id} 初始阅读数={initial_read_num}")

        return True
```

替换为：

```python
        # 保存确认记录，包含初始阅读数
        self._storage.save_join_confirmation(group_id, user_id, nickname, msg_id, initial_read_num)
        logger.info(f"[GroupMgr] 进群确认: 已保存确认记录 群={group_id} 用户={user_id} 初始阅读数={initial_read_num}")

        # 启动进群确认检查任务（事件驱动：有人进群时启动，全部确认后自动停止）
        self._start_join_confirm_check()

        return True
```

- [ ] **Step 2: 在 `main.py` 的 `__init__` 中添加遗留 pending 记录兜底检查**

将 `main.py` 第 101-103 行：

```python
        # 后台调度器（F3 定时解禁 + F2 申诉超时清理）
        self._init_scheduler()
        self._start_scheduler()
```

替换为：

```python
        # 后台调度器（F3 定时解禁 + F2 申诉超时清理）
        self._init_scheduler()
        self._start_scheduler()
        # 兜底：热重载后若有遗留的 pending 进群确认记录，启动检查任务
        try:
            if self._storage.list_pending_confirmations():
                self._start_join_confirm_check()
                logger.info("[GroupMgr] 检测到遗留待确认记录，已启动进群确认检查任务")
        except Exception as e:
            logger.warning(f"[GroupMgr] 检查遗留待确认记录失败: {e}")
```

- [ ] **Step 3: 提交**

```bash
git add membership.py main.py
git commit -m "feat: 接入进群确认检查事件触发与热重载兜底"
```

- [ ] **Step 4: 重启 AstrBot 验证**

重启 AstrBot，检查日志：
- 应看到 `[GroupMgr] 后台调度器` 正常启动（无进群确认检查的 debug 日志刷屏）
- 若有遗留 pending 记录，应看到 `检测到遗留待确认记录，已启动进群确认检查任务`
- 若无遗留记录，不应看到进群确认检查任务的任何日志

- [ ] **Step 5: 功能验证——新用户进群**

在一个启用进群确认功能的群中邀请一个新用户进群，检查：
1. 用户被禁言 + 收到提醒消息
2. 日志出现 `进群确认检查任务已启动`
3. 等待 15 秒后日志**不出现** `进群确认检查: 群=... 当前阅读数=...`（无变化时静默）
4. 用户阅读群公告并点击「确认收到」后，15 秒内自动解禁
5. 日志出现 `已解禁用户`
6. 全部确认后日志出现 `无待确认记录，任务退出`

- [ ] **Step 6: 超时验证（可选）**

将 `join_ban_confirm_check_timeout` 配置为较小值（如 60 秒），让一个用户进群后不确认公告，观察：
1. 60 秒后日志出现 `条记录超时未确认，已标记 timeout`
2. 该用户保持禁言状态
3. 若无其他 pending 记录，任务退出
