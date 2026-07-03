# 进群确认检查：事件驱动按需检查优化设计

**日期**: 2026-07-03
**状态**: 已批准

## 背景与问题

当前进群确认检查逻辑存在以下问题：

1. **持续轮询**：主调度器 `_scheduler_loop` 每 60 秒无条件轮询，每轮都调用 `_check_join_confirmations_by_read_num()`。虽然该方法在无 pending 记录时会早返回，但仍有不必要的 DB 查询和日志噪音。
2. **检查间隔不灵活**：进群确认检查与定时解禁（`_run_due_unbans`）、申诉超时（`_expire_appeals`）共用 60 秒主循环，无法独立配置更短的检查间隔（用户确认公告后需等待最多 60 秒才能解禁）。
3. **pending 记录永不清理**：用户若一直不确认公告，pending 记录永远停留，每次轮询都被遍历，无超时清理机制。

## 优化目标

将进群确认检查从主调度器中剥离，改为**事件驱动的按需独立任务**：

- 用户进群时启动检查任务
- 全部 pending 记录处理完成后（confirmed 或 timeout）任务自动退出
- 检查间隔独立配置（更短，如 15 秒），快速响应用户确认
- 超时未确认的记录标记为 timeout，停止检查

## 设计

### 1. 架构与任务生命周期

#### 架构变更

将 `_check_join_confirmations_by_read_num()` 从主调度器 `_scheduler_loop` 中移除，成为独立 asyncio 任务。

**变更前**：
```
_scheduler_loop (60s)
  ├─ _run_due_unbans()
  ├─ _expire_appeals()
  └─ _check_join_confirmations_by_read_num()  // 移除
```

**变更后**：
```
_scheduler_loop (60s)
  ├─ _run_due_unbans()
  └─ _expire_appeals()

_join_check_task (按需启停, 15s)
  └─ _join_confirm_check_loop()
```

#### 任务启动

在 `membership.py` 的 `_handle_group_increase` 方法末尾（保存确认记录之后），调用 `_start_join_confirm_check()`。

`_start_join_confirm_check()` 逻辑（位于 `scheduler.py`）：
- 若 `_join_check_task` 已存在且未完成 → 跳过（幂等）
- 否则 `asyncio.create_task(self._join_confirm_check_loop())`

#### 任务运行

`_join_confirm_check_loop()` 逻辑（位于 `scheduler.py`，替代原 `_check_join_confirmations_by_read_num`）：

```
while True:
    sleep(join_ban_confirm_check_interval)  # 默认15秒
    try:
        # 1. 超时清理：将超过阈值的 pending 记录标记为 timeout
        self._storage.timeout_pending_confirmations(timeout)

        # 2. 查询剩余 pending 记录
        pending = self._storage.list_pending_confirmations()
        if not pending:
            break  # 全部处理完，任务退出

        # 3. 按群分组检查阅读数（带缓存）
        # 4. current > initial → 解禁 + confirm_join_user
    except CancelledError:
        raise
    except Exception as e:
        consecutive_errors += 1
        if consecutive_errors >= 10:
            break
        continue
    consecutive_errors = 0
```

#### 任务停止

- **自然退出**：`list_pending_confirmations()` 返回空时 `break`
- **插件卸载**：`terminate()` 中 cancel `_join_check_task`（与现有 `_stop_scheduler` 并列）

### 2. 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `join_ban_confirm_check_interval` | int | 15 | 检查任务运行时的轮询间隔（秒）。已有配置项，默认值从 60 改为 15 |
| `join_ban_confirm_check_timeout` | int | 86400 | 检查超时阈值（秒），默认 24 小时。超过此时长未确认的 pending 记录标记为 timeout，不再检查 |

### 3. 存储变更

#### 新增 `timeout` 状态

`join_confirmations.status` 字段新增 `timeout` 值：

| status | 含义 |
|--------|------|
| `pending` | 待确认，检查任务会处理 |
| `confirmed` | 已确认公告，已解禁 |
| `timeout` | 超时未确认，停止检查，用户保持禁言 |

#### 新增存储方法

```python
def timeout_pending_confirmations(self, timeout_seconds: int) -> int:
    """将超过 timeout_seconds 未确认的 pending 记录标记为 timeout。
    返回更新的记录数。"""
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

#### 已有方法无需修改

- `list_pending_confirmations()` 已按 `status='pending'` 过滤，timeout 记录自动排除
- `confirm_join_user()` 已按 `status='pending'` 过滤，不会误操作 timeout 记录
- `save_join_confirmation()` 用 `INSERT OR REPLACE`，用户退群再进群会覆盖旧记录（包括 timeout→pending 重置）

### 4. 异常处理与边界情况

#### 任务异常恢复

检查循环捕获异常，避免任务意外退出后无人重启：
- 连续 10 次出错则任务退出
- 任务退出后，下一个用户进群时会重新启动（`_start_join_confirm_check` 的幂等检查发现任务已结束，重新创建）

#### 边界情况

| 场景 | 处理方式 |
|------|----------|
| 插件热重载 | `terminate()` cancel 旧任务；`__init__` 时检查是否有遗留 pending 记录，有则启动检查任务（兜底） |
| 用户退群再进群 | `save_join_confirmation` 的 `INSERT OR REPLACE` 覆盖旧记录（timeout→pending 重置），检查任务已在运行则直接覆盖 |
| 群关闭进群确认功能 | 检查循环中 `join_ban_confirm_enabled` 为 false 时跳过该群记录。记录不会被主动清理 |
| `_get_group_notice_read_num` 返回 0 | `0 > initial_read_num` 为 false，不会误判。保持现有行为 |
| 解禁失败 | 记录保持 pending，下一轮重试。无重试次数上限（现有逻辑保留） |

#### 日志优化

- 循环运行时：仅在有状态变化（解禁/超时）时输出 `info` 日志，无变化时不输出
- 任务启动/停止时输出 `info` 日志，便于排查

## 涉及文件

| 文件 | 变更内容 |
|------|----------|
| `scheduler.py` | 新增 `_start_join_confirm_check()`、`_join_confirm_check_loop()`；从 `_scheduler_loop` 移除进群确认检查调用；`_stop_scheduler` 中 cancel `_join_check_task` |
| `membership.py` | `_handle_group_increase` 末尾调用 `_start_join_confirm_check()` |
| `storage.py` | 新增 `timeout_pending_confirmations()` 方法 |
| `_conf_schema.json` | 新增 `join_ban_confirm_check_timeout` 配置项；`join_ban_confirm_check_interval` 默认值改为 15 |
| `main.py` | `__init__` 中增加遗留 pending 记录兜底检查 |

## 不在本次范围

- 修复 `notices[0]` 公告 ID 锁定问题（独立问题，另行处理）
- 同群批量解禁语义改进（需要 OneBot 提供更细粒度接口）
- confirmed/timeout 记录的历史清理（可后续增加定期清理任务）
