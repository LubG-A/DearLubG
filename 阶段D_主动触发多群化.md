# 阶段 D：主动触发多群化（Per-Group Active Scheduler）

**目标**：把主动触发从"全局单例调度器 + 硬编码第一个群"改造为"每群独立调度器"。每群维护独立的 active 倒计时，间隔沿用原有随机 min-max 逻辑。

**约束**：不破坏 cycle 串行机制；不破坏 quiet_window 兜底；不破坏 per-group 历史独立性；保留深夜跳过逻辑。

**验证标准**：多群环境下，每群独立倒计时；各群定时器互不干扰；深夜不触发；cycle 撞锁时正确入队。

---

## 一、背景与动机

### 阶段 C 留下的"主动触发全局化"

阶段 B 为了聚焦多群配置和 cycle 队列，关闭了主动触发（`config.active_trigger.enabled: false`），代码保留但 `on_active_trigger` 硬编码 `group_ids[0]`。阶段 C 完成了跨群印记板，不影响主动触发。现在需要补齐最后的主动触发多群化。

### 现状问题

1. **ActiveScheduler 是全局单例**（`Bot.scheduler`）：状态字段 `last_active_check_time` / `next_active_check_time` 是单一 datetime，不是 per-group 字典；状态文件 `state/scheduler.json` 是全局的；`_callback` 不传 group_id，`on_active_trigger` 只能硬编码第一个群。
2. **`on_active_trigger` 硬编码 `group_ids[0]`**：多群下只有第一个群会被主动触发，其他群永远不会被主动触发。

### 本阶段范围

本阶段仅实现"每群独立倒计时"，间隔沿用原有随机 min-max 逻辑（`_generate_next_check_time` 不改）。

**群活跃度自适应**（活跃的群多触发、冷清的群少触发）留待未来方案，不在本阶段范围内。

### 与 quiet_window 的关系

`quiet_window`（150s 静默窗口）和 `active_trigger`（主动触发定时器）是两个独立的 per-group 定时器：

| | quiet_window | active_trigger |
|---|---|---|
| 触发条件 | 150s 无新消息 | 定时器到期 |
| 目的 | 兜底：攒了一波消息该看了 | 主动：没人说话时偶尔冒泡 |
| is_active | False（被动） | True（主动） |
| 间隔 | 固定 150s（每条消息重置） | 随机 min-max 分钟 |

两者各自独立运行，靠 cycle 串行机制（`_try_enter_cycle`）去重——如果 quiet_window 已经触发了 cycle，active_trigger 到期时会撞 `_cycle_running` 入队等待。

---

## 二、设计决策与权衡

### 决策 D1：调度器归属——per-group 实例，移入 GroupContext

**方案 A（全局单例 + per-group 状态字典）**：ActiveScheduler 保持单例，内部维护 `{group_id: {...}}` 字典，用一个 Timer 轮询所有群。
**方案 B（per-group 实例，移入 GroupContext）**：每个 GroupContext 持有自己的 ActiveScheduler 实例，独立 Timer，独立状态。

**采用方案 B**。理由：
1. **与 `_quiet_timer` 模式一致**：`_quiet_timer` 已经是 per-group 的（存在 GroupContext 里），active_trigger 用同样的模式，架构一致。
2. **复用 ActiveScheduler 的逻辑**：深夜跳过、状态持久化、间隔计算等逻辑已经实现且测试过，无需重写。
3. **隔离性好**：每群独立 Timer，互不干扰，一个群的调度器异常不影响其他群。
4. **阶段 B 文档已明确方向**：`阶段B文档` 决策 B6 写道"阶段 D 会把 scheduler 移入 GroupContext，实现每群独立倒计时"。
5. **比方案 A 简单**：方案 A 需要一个轮询线程定期检查所有群的到期时间，要么引入轮询延迟，要么状态管理复杂化。方案 B 每群一个 Timer，最直接。

**改造方式**：
- `GroupContext.__init__` 新增 `self.active_scheduler = ActiveScheduler(config, group_id=group_id, state_dir=per_group_state_dir)`。
- `Bot.scheduler` 全局单例删除。
- 状态文件从 `state/scheduler.json` 移到 `state/{group_id}/scheduler.json`。

### 决策 D2：间隔计算——沿用原有随机 min-max

**采用**：`_generate_next_check_time` 保持原有逻辑不变——在 `[min_interval_minutes, max_interval_minutes]` 区间内随机取值，跳过深夜时段。

理由：
1. 本阶段目标是"支持多群"，不涉及间隔优化。
2. 活跃度自适应留待未来方案，避免本阶段复杂度膨胀。
3. 原有逻辑已实现且测试过，无需改动。

### 决策 D3：主动触发到期时的处理——直接 is_active=True

**采用**：主动触发定时器到期时，直接走 `_run_llm_cycle(is_active=True)`，不判断 pending 状态。

理由：
1. **简单**：不需要额外判断 pending 状态，直接走标准流程。
2. **语义正确**：主动触发的目的是"让 LLM 看一眼要不要开口"，即使有 pending 消息也可以看一眼（LLM 会同时看到新消息 + 主动检查状态标记，自主决定 silent 还是 reply）。
3. **cycle 串行机制兜底**：如果主动触发到期时正好有 quiet_window 触发的 cycle 在跑，`_try_enter_cycle` 会入队等待，不会冲突。

### 决策 D4：状态持久化——per-group scheduler.json

**采用**：状态文件从全局 `state/scheduler.json` 移到 `state/{group_id}/scheduler.json`。

理由：
1. **与 per-group 架构一致**：conversation.json、state.json、attribution_log.jsonl 都在 `state/{group_id}/` 下，scheduler.json 应该一致。
2. **隔离性好**：删除某群时，删 `state/{group_id}/` 目录即可，无需编辑全局文件。
3. **阶段 B 文档已明确**：`阶段B文档` 的目标存储结构图标注 `scheduler.json 全局（阶段 D 移入子目录）`。

**数据迁移**：不迁移。阶段 B 期间主动触发关闭，`state/scheduler.json` 里的数据是旧数据，已过时。启动时直接重新生成 per-group 状态。

### 决策 D5：on_active_trigger 改造——per-group 回调

**现状**：`on_active_trigger(self)` 不接受 group_id，硬编码 `group_ids[0]`。

**改造**：`on_active_trigger(self, group_id: str)` 接受 group_id 参数，由 per-group 调度器的 Timer 直接传入。

**调度器回调签名变化**：
- 现状：`start(callback: Callable[[], None])` → 回调无参数
- 改造后：`start(callback: Callable[[str], None], group_id: str)` → 调度器在 `_fire` 时调用 `callback(group_id)`

### 决策 D6：深夜跳过——保留现有逻辑

**采用**：保留 `_generate_next_check_time` 和 `_fire` 中的深夜跳过逻辑（23:00-3:00 不触发，顺延到次日 3:00）。

理由：真人深夜睡觉，不会主动在群里说话。逻辑已实现且测试过，无需改动。

### 决策 D7：启动初始化——warmup 时为每群启动调度器

**采用**：`warmup` 中遍历 `self.groups`，为每个群调用 `ctx.active_scheduler.start(self.on_active_trigger, group_id)`。

理由：
1. 与 quiet_timer 的初始化时机一致（warmup 时 GroupContext 已创建）。
2. 如果 `config.active_trigger.enabled: false`，不启动调度器（与现有逻辑一致）。

### 决策 D8：与印记板的协同——无特殊处理

**采用**：主动触发 cycle 成功后，照常调用 `impression_board.maybe_update`（阶段 C 已实现）。印记板更新不感知触发类型（被动/主动）。

理由：主动触发也是一次有效的 LLM cycle，应该计入 trigger_count。

---

## 三、具体改动清单

### 3.1 修改 `src/scheduler.py`

#### 3.1.1 构造函数新增 `group_id` 参数

```python
def __init__(self, config: Config, group_id: str, state_dir: str = "state"):
    self.config = config
    self.group_id = group_id            # 阶段 D 新增
    # ... 其余字段不变 ...
```

#### 3.1.2 `start` 接受 `group_id` 参数

```python
def start(self, callback: Callable[[str], None], group_id: str):
    """启动调度器。

    Args:
        callback: 主动触发回调，签名 (group_id) -> None
        group_id: 本调度器所属的群 ID
    """
    self._callback = callback
    self.group_id = group_id
    # ... 其余逻辑不变 ...
```

#### 3.1.3 `_fire` 传 group_id 给回调

```python
def _fire(self):
    # ... 深夜检查（保留现有逻辑，日志加 group_id）...
    logger.info(f"群 {self.group_id} 主动触发定时器到期，执行回调")
    self.last_active_check_time = now
    self._save()

    try:
        if self._callback:
            self._callback(self.group_id)      # 阶段 D：传 group_id
    except Exception as e:
        logger.error(f"群 {self.group_id} 主动触发回调异常: {e}", exc_info=True)
    finally:
        self._schedule_next()
```

#### 3.1.4 `_generate_next_check_time` 不变

间隔计算逻辑（随机 min-max + 深夜跳过）保持原样。

#### 3.1.5 日志加 group_id 前缀

所有日志中的"主动触发"相关消息加上 `群 {self.group_id}` 前缀，便于多群区分。

### 3.2 修改 `src/group_context.py`

`GroupContext.__init__` 新增 `active_scheduler`：

```python
from .scheduler import ActiveScheduler

# 在 __init__ 中：
per_group_state_dir = f"{state_dir}/{group_id}"
self.active_scheduler = ActiveScheduler(config, group_id=group_id, state_dir=per_group_state_dir)
```

### 3.3 修改 `main.py`

#### 3.3.1 删除全局 scheduler 单例

```python
# 删除：
# self.scheduler = ActiveScheduler(self.config)  # 阶段 D 移入 GroupContext
```

#### 3.3.2 `warmup` 中为每群启动调度器

```python
def warmup(self):
    # ... 现有逻辑 ...

    # 阶段 D：为每群启动主动触发调度器
    if self.config.active_trigger and self.config.active_trigger.enabled:
        for group_id, ctx in self.groups.items():
            ctx.active_scheduler.start(self.on_active_trigger, group_id)
        logger.info(f"主动触发调度器已启动：{len(self.groups)} 个群，"
                    f"间隔 {self.config.active_trigger.min_interval_minutes}-"
                    f"{self.config.active_trigger.max_interval_minutes} 分钟随机，"
                    f"深夜 {self.config.active_trigger.night_start_hour}:00-"
                    f"{self.config.active_trigger.night_end_hour}:00 禁用")
    else:
        logger.info("主动触发调度器已禁用")
```

#### 3.3.3 `on_active_trigger` 改为接受 group_id

```python
def on_active_trigger(self, group_id: str):
    """主动触发回调（由 per-group 调度器调用）。

    阶段 D：每群独立调度器触发，传入对应 group_id。
    """
    try:
        logger.info(f"群 {group_id} 主动触发：执行 LLM 调用")
        ctx = self.groups.get(group_id)
        if ctx is None:
            logger.warning(f"主动触发但未找到群 {group_id} 的上下文")
            return

        if not self._try_enter_cycle(ctx):
            logger.debug(f"群 {group_id} 主动触发但 cycle 在跑，已入队等待")
            return

        try:
            self._run_llm_cycle(ctx, soft_factors=None, is_active=True)
        finally:
            self._exit_cycle()
            self._drain_cycle_queue()
    except Exception as e:
        logger.error(f"群 {group_id} 主动触发异常: {e}", exc_info=True)
```

#### 3.3.4 `run()` 中删除全局 scheduler 启动逻辑

```python
def run(self, webhook_host: str = "0.0.0.0", webhook_port: int = 8081):
    """启动机器人。"""
    self.warmup()   # 阶段 D：scheduler 启动移入 warmup
    server = NapCatWebhookServer(
        webhook_host, webhook_port, self.on_group_message,
        on_recall=self.on_group_recall,
        on_poke=self.on_group_poke,
    )
    logger.info(f"机器人启动，监听群 {list(self.groups.keys())}")
    server.start()
```

### 3.4 修改 `config.yaml`

```yaml
active_trigger:
  enabled: true                          # 阶段 D 开启
  # 其余配置不变
```

### 3.5 不需要修改的文件

| 文件 | 原因 |
|---|---|
| `src/config.py` | 不新增配置项（活跃度自适应留待未来） |
| `src/impression.py` | 印记板不感知触发类型（决策 D8） |
| `src/persona.py` | is_active prompt 渲染不变 |
| `src/history.py` | 无关 |
| `src/trigger.py` | 被动触发评分不涉及主动触发 |
| `src/llm_client.py` | 无关 |
| `src/napcat_client.py` | 无关 |
| `src/senders/*` | 无关 |
| `src/attribution.py` | 主动触发时 soft_factors=None，归因跳过（现有逻辑） |

### 3.6 数据迁移

- `state/scheduler.json`（全局旧文件）：启动时忽略，不迁移。理由：阶段 B 期间主动触发关闭，旧数据已过时。
- `state/{group_id}/scheduler.json`（per-group 新文件）：不存在则由 `ActiveScheduler._load` 初始化为空，`start` 时生成新的 `next_active_check_time`。

---

## 四、验证方法

### 4.1 单群启动验证

config.yaml 配置 `enabled: true`，单群启动：
1. 日志输出"主动触发调度器已启动：1 个群"。
2. `state/{group_id}/scheduler.json` 生成，含 `last_active_check_time`（null）和 `next_active_check_time`。
3. 等待定时器到期，日志输出"群 xxx 主动触发定时器到期，执行回调"。
4. LLM 调用成功，日志含 `[主动触发]` 标记。

### 4.2 多群独立调度验证

1. 配置 4 个群，启动。
2. 检查 `state/{group_id}/scheduler.json` 各自独立，`next_active_check_time` 各不相同（随机间隔）。
3. 某群先触发时，其他群的调度器不受影响。
4. 日志中每条主动触发消息都带 `群 {group_id}` 前缀，可区分。

### 4.3 深夜跳过验证

1. 手动修改 `state/{group_id}/scheduler.json` 的 `next_active_check_time` 为凌晨 1:00。
2. 等待到 1:00，日志输出"群 xxx 当前为深夜，跳过本次主动触发，重新调度"。
3. `next_active_check_time` 更新为次日 3:00 之后。

### 4.4 cycle 撞锁验证

1. 群 A 正在被动触发 cycle（quiet_window 兜底），群 A 的主动触发定时器同时到期。
2. 日志输出"群 A 主动触发但 cycle 在跑，已入队等待"。
3. 被动 cycle 完成后，`_drain_cycle_queue` 处理群 A 的主动触发（is_active=True）。

### 4.5 状态持久化验证

1. 主动触发一次后，kill 进程。
2. 重启，检查 `state/{group_id}/scheduler.json` 的 `next_active_check_time` 被恢复。
3. 调度器从恢复的时间点继续倒计时（不会从头开始）。

### 4.6 日志关键词检查

- `主动触发调度器已启动：N 个群`（启动时）
- `群 xxx 主动触发定时器到期，执行回调`
- `群 xxx 当前为深夜，跳过本次主动触发，重新调度`
- `群 xxx 主动触发但 cycle 在跑，已入队等待`

不应出现：
- 全局 `state/scheduler.json` 被读写（应只读写 per-group 文件）
- `on_active_trigger` 硬编码 `group_ids[0]`（应接受 group_id 参数）
- 所有群同时触发（应各自独立倒计时）

---

## 五、不在本次范围内的事项（未来方案）

| 事项 | 阶段 |
|---|---|
| **群活跃度自适应**：根据群近期消息频率动态调整主动触发间隔（活跃的群多触发、冷清的群少触发） | 未来 |
| per-group 深夜时段配置（不同群不同静默时间） | 未来 |
| 主动触发时注入群活跃度到 prompt（让 LLM 知道"这个群最近很活跃/冷清"） | 未来 |
| 主动触发的"开口意愿"受印记板影响（如别群在聊相关话题时更想开口） | 未来 |
| per-group 配置不同的 min/max 间隔 | 未来 |
| 主动触发频率的运行时统计与调优面板 | 不做 |

---

## 六、风险与回滚

### 风险

1. **主动触发发言过多**：min_interval=60min，活跃群每小时主动触发一次。
   - 分析：主动触发时 LLM 会看到"主动检查"状态，可以选择 silent。间隔短只是增加"看一眼"的机会，不等于"一定开口"。
   - 缓解：如果发言过多，调大 min_interval 或在 config.yaml 关闭 enabled。

2. **ActiveScheduler 实例化失败阻塞启动**：如果某群的 `state/{group_id}/` 目录创建失败，ActiveScheduler 构造会抛异常。
   - 缓解：`state_dir.mkdir(parents=True, exist_ok=True)` 已经容错。若仍失败，说明系统有问题，阻塞启动可接受。

### 回滚

阶段 D 回滚步骤：
1. `git revert` 代码改动。
2. `config.yaml` 改回 `enabled: false`。
3. 删除 `state/{group_id}/scheduler.json`（可选，留着也不影响）。
4. Bot 行为回到阶段 C（主动触发关闭，per-group 印记板正常工作）。

回滚成本低：ActiveScheduler 改动是独立的，不影响印记板/history/attribution/trigger 任何现有功能。删除全局 `Bot.scheduler` 引用后，warmup 不再启动调度器，被动触发和 quiet_window 兜底不受影响。
