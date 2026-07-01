# 阶段 A：GroupContext 重构

**目标**：把 Bot 中散落的 per-group 状态抽取到 `GroupContext` 容器，为多群支持搭好结构骨架。
**约束**：纯重构，单群运行，零行为变化。重构后 bot 的所有可观测行为（触发、回复、压缩、归因、亲密度、主动触发、延迟回复、撤回/戳一戳）与重构前完全一致。
**验证标准**：用现有 `config.yaml` 启动 bot，跑一轮完整的被动触发 + 主动触发，日志和行为与重构前不可区分。

---

## 一、背景与动机

### 当前架构的问题

当前 `Bot` 类（[main.py](file:///d:/WorkSpace/DearLubG/main.py)）是一个"巨对象"：既持有全局单例（napcat、llm、persona、affinity、scheduler、senders），又持有 per-group 状态（history、attribution、trigger_evaluator、last_reply_time、quiet_timer、cycle_pending）。所有方法直接访问 `self.history`、`self.last_reply_time` 等，群号硬编码在 `self.config.napcat.group_id`。

这导致：
1. **无法支持多群**：所有 per-group 状态只有一份，无法按群隔离。
2. **职责模糊**：Bot 既是全局协调者，又是单个群的执行者，两种职责耦合在一个类里。
3. **未来改造成本高**：阶段 B（多群配置）需要在每个方法里加 `group_id` 参数并查找对应状态，改动分散且易漏。

### 阶段 A 的定位

阶段 A 只做**结构抽取**，不做行为改变：
- 把 per-group 状态从 Bot 移到 `GroupContext`。
- Bot 持有 `groups: dict[str, GroupContext]`，启动时为 config 中的单个 group_id 构造一个 GroupContext。
- Bot 方法通过 `ctx = self.groups[group_id]` 访问 per-group 状态，群号来自 ctx 而非 config。
- 文件名、NapCatClient、webhook 过滤等**暂不改**（仍是单群），留给阶段 B。

这样阶段 B 只需：① 改 config 支持多 group_id；② 改 NapCatClient 方法接受 group_id 参数；③ 改 webhook 去掉单群过滤；④ 给 HistoryManager/AttributionManager 文件名加 group_id 后缀。阶段 A 搭好的 GroupContext 结构不需要再动。

---

## 二、设计决策与权衡

### 决策 A1：GroupContext 作为数据容器 vs 带方法的完整对象

**方案 1（数据容器）**：GroupContext 只持有数据字段（history、attribution、trigger_evaluator、last_reply_time、quiet_timer、cycle_pending），方法留在 Bot 并接受 `ctx` 参数。

**方案 2（完整对象）**：GroupContext 持有数据 + 方法（on_group_message、_run_llm_cycle、_handle_result 等），Bot 只做全局协调和路由。

| 维度 | 方案 1（数据容器） | 方案 2（完整对象） |
|---|---|---|
| 改动量 | 小（方法签名加 ctx 参数） | 大（方法整体搬迁） |
| 风险 | 低（逻辑不变，只是换访问路径） | 中（方法搬迁可能引入隐蔽 bug） |
| OOP 纯度 | 低（Bot 仍然很胖） | 高（职责清晰） |
| 阶段 B 扩展性 | 够用（多群 = 多个 ctx 实例） | 更好（GroupContext 自包含） |

**采用方案 1**。理由：
1. 阶段 A 的核心目标是"搭结构骨架"，不是"完美 OOP"。方案 1 以最小改动达成目标，风险最低。
2. 方案 2 的好处在阶段 B/C/D 才会充分体现，当前提前搬迁反而增加验证负担——难以区分"行为变化"和"搬迁引入的 bug"。
3. 方案 1 不阻碍后续向方案 2 演进：阶段 B 之后可以逐步把方法从 Bot 移入 GroupContext，每次移动一个方法并独立验证。

### 决策 A2：HistoryManager / AttributionManager 文件名是否立即参数化

**方案 A（立即参数化）**：`HistoryManager.__init__` 加 `group_id` 参数，文件名变为 `conversation_{group_id}.json`。需要一次性迁移现有 `conversation.json`。

**方案 B（暂不参数化）**：阶段 A 保持 `conversation.json` / `state.json` 文件名不变。阶段 B 再加 `group_id` 参数并迁移。

**采用方案 B**。理由：
1. 阶段 A 承诺"零行为变化"，文件名改了会导致重启后加载不同文件，需要迁移逻辑，引入风险。
2. 阶段 B 本来就要改文件名（加 group_id 后缀），届时一次性做迁移更合理。
3. GroupContext 持有 HistoryManager 实例即可，不关心文件名——阶段 B 改 HistoryManager 时 GroupContext 无需改动。

### 决策 A3：NapCatClient 是否立即支持多群

当前 NapCatClient 在 `__init__` 存储 `self.group_id`，方法内部直接用。`member_cache` 是扁平 `{qq: info}`。

**方案 A（立即重构）**：方法改为接受 `group_id` 参数，`member_cache` 改为嵌套 `{group_id: {qq: info}}`，`warmup` 接受 group_id 列表。

**方案 B（暂不重构）**：阶段 A 保持 NapCatClient 原样，仍是单群。Bot 通过 `self.napcat` 访问，group_id 仍来自 config。

**采用方案 B**。理由：
1. NapCatClient 的重构属于"协议层适配多群"，与 GroupContext 抽取是正交的两件事。
2. 阶段 A 的 GroupContext 不持有 NapCatClient（它是全局共享的），所以 NapCatClient 改不改不影响 GroupContext 的结构。
3. 阶段 B 改 NapCatClient 时，Bot 方法已经在用 `ctx.group_id`，只需把 `ctx.group_id` 传给 NapCatClient 方法即可，改动集中且清晰。

### 决策 A4：`_cycle_pending` 归属

当前 `_cycle_pending` 是 Bot 的单例 bool。多群后，群 A 的 cycle 期间，群 B 触发撞 cycle，应该标记的是"群 B 有 pending"而非"群 A 有 pending"。

**采用**：`_cycle_pending` 移入 GroupContext（每群一个）。`_cycle_running` 保留在 Bot（全局串行）。

阶段 A 虽然只有一个群，但结构上 `_cycle_pending` 属于 per-group 状态，移入 GroupContext 是正确的。当前 while 循环检查 `_consume_pending_trigger(ctx)` 即可。

> 注：阶段 B 需要新增"cycle 结束后扫描所有群的 _cycle_pending"逻辑（全局队列），阶段 A 不需要。

### 决策 A5：哪些状态留在 Bot（全局）

根据之前讨论确认的归属表：

| 状态 | 归属 | 阶段 A 处理 |
|---|---|---|
| conversation/buffer/pending/delayed | 每群 | 移入 GroupContext |
| attribution（dynamic_weight） | 每群 | 移入 GroupContext |
| trigger_evaluator | 每群 | 移入 GroupContext |
| last_reply_time | 每群 | 移入 GroupContext |
| quiet_timer / quiet_timer_lock | 每群 | 移入 GroupContext |
| _cycle_pending | 每群 | 移入 GroupContext |
| affinity（按 QQ 全局） | 全局 | 留在 Bot |
| napcat / llm / persona_renderer | 全局 | 留在 Bot |
| senders（message/voice/image/emoji） | 全局 | 留在 Bot |
| scheduler（主动触发） | 全局* | 留在 Bot（阶段 D 移入 GroupContext） |
| _cycle_running / _cycle_lock | 全局 | 留在 Bot |
| self_qq / self_nickname | 全局 | 留在 Bot |

> *scheduler 在阶段 A 留在 Bot。阶段 D（主动触发按群独立倒计时）时移入 GroupContext。当前阶段单群，留在 Bot 行为不变。

### 决策 A6：GroupContext 持有共享单例的引用 vs 通过 Bot 中转

GroupContext 需要访问 napcat（get_nickname）、llm（summarizer）、affinity（get/apply_delta）。两种方式：

**方案 A（持有引用）**：GroupContext 在 `__init__` 接收 napcat、llm、affinity 的引用并存储。
**方案 B（通过 Bot 中转）**：GroupContext 只持有 group_id，需要访问共享单例时回调 Bot。

**采用方案 A**。理由：
1. 引用注入是标准做法，依赖关系清晰。
2. 避免每次访问共享单例都要经过 Bot 中转，减少耦合。
3. GroupContext 持有的是引用（不是副本），不会增加内存开销。

---

## 三、具体改动清单

### 3.1 新增 `src/group_context.py`

新文件，定义 `GroupContext` 类。

```python
"""群上下文容器（per-group state container）。

持有单个群的所有独立状态：
- conversation history（messages, pending, fast_buffer, delayed_replies）
- attribution（dynamic_weight 按群独立）
- trigger_evaluator（使用本群 history + 全局 affinity）
- last_reply_time（本群冷却计时）
- quiet_timer（本群静默窗口）
- cycle_pending（本群撞 cycle 重试标志）

全局共享单例（napcat, llm, affinity, persona）由 Bot 注入引用。
"""
import threading
from typing import Optional

from .config import Config
from .history import HistoryManager
from .attribution import AttributionManager
from .trigger import TriggerEvaluator
from .affinity import AffinityManager
from .napcat_client import NapCatClient
from .llm_client import LLMClient


class GroupContext:
    """单个群的上下文容器。"""

    def __init__(
        self,
        group_id: str,
        config: Config,
        napcat: NapCatClient,
        llm: LLMClient,
        affinity: AffinityManager,
        self_qq: str,
        persona_name: str,
    ):
        self.group_id = group_id
        self.config = config

        # 共享单例引用（不拷贝）
        self.napcat = napcat
        self.llm = llm
        self.affinity = affinity

        # per-group 状态
        self.history = HistoryManager(config.trigger)
        self.history.set_summarizer(llm.summarize)
        self.attribution = AttributionManager(config)
        self.trigger_evaluator = TriggerEvaluator(
            config, self.history, self_qq, persona_name,
            affinity_manager=affinity,
        )

        # per-group 运行时状态
        self.last_reply_time: float = 0.0
        self._quiet_timer: Optional[threading.Timer] = None
        self._quiet_timer_lock = threading.Lock()
        self._cycle_pending: bool = False
```

**说明**：
- `HistoryManager` 和 `AttributionManager` 仍用默认文件名（`conversation.json` / `state.json`），阶段 B 再参数化。
- `set_summarizer` 在构造时立即调用，与当前 Bot.__init__ 行为一致。
- 不持有 persona_renderer（system prompt 渲染留 in Bot，因为 persona 是全局的，且需要 summary + impressions 拼接，阶段 A 不改这个逻辑）。
- 不持有 senders（发送留 in Bot，因为 senders 是全局共享的）。
- 不持有 scheduler（阶段 D 再移入）。

### 3.2 修改 `main.py`

这是改动最大的文件。核心是把 `self.history`、`self.attribution`、`self.trigger_evaluator`、`self.last_reply_time`、`self._quiet_timer*`、`self._cycle_pending` 的访问改为通过 `ctx` 中转。

#### 3.2.1 `Bot.__init__` 改动

**删除**（移入 GroupContext）：
- `self.history`
- `self.attribution`
- `self.trigger_evaluator`
- `self.last_reply_time`
- `self._quiet_timer`
- `self._quiet_timer_lock`
- `self._cycle_pending`

**保留**（全局）：
- `self.config`, `self.napcat`, `self.llm`, `self.affinity`, `self.persona_renderer`, `self.scheduler`
- `self.message_sender`, `self.ai_voice_sender`, `self.local_voice_sender`, `self.image_sender`, `self.emoji_reactor`
- `self.self_qq`, `self.self_nickname`
- `self._cycle_lock`, `self._cycle_running`

**新增**：
- `self.groups: dict[str, GroupContext] = {}`  — 群上下文字典，启动时填充

改后的 `__init__` 结构：
```python
def __init__(self, config_path: str = "config.yaml"):
    self.config = load_config(config_path)

    # 全局共享单例
    self.napcat = NapCatClient(self.config.napcat.base_url, self.config.napcat.group_id)
    self.llm = LLMClient(self.config)
    self.affinity = AffinityManager()
    self.persona_renderer = PersonaRenderer(self.config)
    self.scheduler = ActiveScheduler(self.config)

    # Sender 实现（全局共享）
    self.message_sender = NapCatMessageSender(self.napcat)
    self.ai_voice_sender = AIRecordVoiceSender(...)
    self.local_voice_sender = LocalFileVoiceSender(self.napcat)
    self.image_sender = EmptyImageSender()
    self.emoji_reactor = EmojiReactor(self.napcat)

    # 全局运行时状态
    self.self_qq: str = ""
    self.self_nickname: str = ""
    self._cycle_lock = threading.Lock()
    self._cycle_running: bool = False

    # per-group 上下文（warmup 后填充）
    self.groups: dict[str, GroupContext] = {}
```

注意：`self.trigger_evaluator` 不再在 `__init__` 创建（它依赖 `self.self_qq`，要等 warmup 后才有）。改为在 `warmup()` 中创建 GroupContext 时一并构造。

#### 3.2.2 `Bot.warmup` 改动

当前 warmup 创建 `trigger_evaluator`。改后 warmup 创建 GroupContext：

```python
def warmup(self):
    self.napcat.warmup()
    self.self_qq = str(self.napcat.self_info.get("user_id", ""))
    self.self_nickname = self.napcat.self_info.get("nickname", "")

    # 为 config 中的 group_id 创建 GroupContext
    group_id = self.config.napcat.group_id
    ctx = GroupContext(
        group_id=group_id,
        config=self.config,
        napcat=self.napcat,
        llm=self.llm,
        affinity=self.affinity,
        self_qq=self.self_qq,
        persona_name=self.config.persona.name,
    )
    self.groups[group_id] = ctx

    self._probe_ai_voice_character()
    logger.info(f"预热完成，机器人 {self.self_nickname}({self.self_qq})，群 {group_id}")
```

#### 3.2.3 `Bot.on_group_message` 改动

当前直接用 `self.history`、`self.trigger_evaluator`。改为先路由到 ctx：

```python
def on_group_message(self, msg: dict):
    try:
        msg_group_id = str(msg.get("group_id", ""))
        # 防御性群过滤（阶段 A 仍保留，阶段 B 去掉）
        if msg_group_id and msg_group_id != self.config.napcat.group_id:
            logger.debug(f"on_group_message 丢弃非目标群消息：{msg_group_id}")
            return

        ctx = self.groups.get(msg_group_id)
        if ctx is None:
            logger.warning(f"未找到群 {msg_group_id} 的上下文，丢弃消息")
            return

        sender_qq = str(msg.get("user_id", ""))
        sender_nick = self.napcat.get_nickname(sender_qq)
        content = msg.get("raw_message", "") or _extract_text_from_msg(msg)
        msg_id = str(msg.get("message_id", ""))

        has_voice = self._has_voice_segment(msg)
        if has_voice:
            content = (content + (" " if content else "") + "[语音消息]").strip()

        # 1. 入 fast_buffer（用 ctx.history）
        ctx.history.append_group_message(sender_qq, sender_nick, content, msg_id)

        if has_voice and msg_id:
            threading.Thread(target=self._transcribe_voice_async, args=(ctx, msg_id,), daemon=True).start()

        # 2. 评分（用 ctx.trigger_evaluator）
        score, soft_factors = ctx.trigger_evaluator.evaluate(msg)
        is_hard = self._is_hard_trigger(ctx, msg, content)
        logger.debug(f"消息评分={score} hard={is_hard} soft_factors={[f.name for f in soft_factors]}")

        # 3. 触发决策（用 ctx 的 last_reply_time）
        if is_hard:
            self._try_trigger_immediate(ctx, hard=True)
        elif ctx.trigger_evaluator.should_peek(score):
            self._try_trigger_immediate(ctx, hard=False, soft_factors=soft_factors)

        # 4. 重置静默定时器（用 ctx 的 timer）
        self._reschedule_quiet_trigger(ctx)

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)
```

#### 3.2.4 `Bot._is_hard_trigger` 改动

当前用 `self.trigger_evaluator`。改为接受 ctx：

```python
def _is_hard_trigger(self, ctx: GroupContext, msg: dict, content: str) -> bool:
    if ctx.trigger_evaluator._check_at_me(msg):
        return True
    if ctx.trigger_evaluator._check_question_to_me(content):
        return True
    return False
```

#### 3.2.5 `Bot._transcribe_voice_async` 改动

当前用 `self.history.update_group_message_content`。改为接受 ctx：

```python
def _transcribe_voice_async(self, ctx: GroupContext, msg_id: str):
    # ... 同现有逻辑，但 self.history -> ctx.history ...
```

#### 3.2.6 `Bot._try_trigger_immediate` 改动

当前用 `self.last_reply_time` 和 `self._cycle_pending`。改为接受 ctx：

```python
def _try_trigger_immediate(self, ctx: GroupContext, hard: bool = False, soft_factors=None):
    now = time.time()
    elapsed = now - ctx.last_reply_time  # 改这里
    cooldown = HARD_COOLDOWN_SECONDS if hard else SOFT_COOLDOWN_MIN
    if elapsed < cooldown:
        logger.debug(f"{'硬' if hard else '软'}因子触发但冷却中（elapsed={elapsed:.1f}s < {cooldown}s）")
        return

    if not self._try_enter_cycle():
        ctx._cycle_pending = True  # 改这里：标记到 ctx 而非 self
        logger.debug(f"{'硬' if hard else '软'}因子触发但 cycle 在跑，已注册 ctx._cycle_pending")
        return

    try:
        self._run_llm_cycle(ctx, soft_factors=soft_factors, is_active=False)
    finally:
        self._exit_cycle()
```

#### 3.2.7 `Bot._reschedule_quiet_trigger` 改动

当前用 `self._quiet_timer` 和 `self._quiet_timer_lock`。改为接受 ctx：

```python
def _reschedule_quiet_trigger(self, ctx: GroupContext, delay: float = QUIET_WINDOW_SECONDS):
    with ctx._quiet_timer_lock:
        if ctx._quiet_timer is not None:
            ctx._quiet_timer.cancel()
        timer = threading.Timer(delay, self._on_quiet_timeout, args=(ctx,))
        timer.daemon = True
        timer.start()
        ctx._quiet_timer = timer
```

#### 3.2.8 `Bot._on_quiet_timeout` 改动

当前用 `self.last_reply_time`。改为接受 ctx：

```python
def _on_quiet_timeout(self, ctx: GroupContext):
    try:
        now = time.time()
        elapsed = now - ctx.last_reply_time  # 改这里
        if elapsed < SOFT_COOLDOWN_MIN:
            remaining = SOFT_COOLDOWN_MIN - elapsed
            logger.debug(f"静默窗口到期但软冷却中，{remaining:.1f}s 后再触发")
            self._reschedule_quiet_trigger(ctx, delay=remaining)
            return

        if not self._try_enter_cycle():
            ctx._cycle_pending = True  # 改这里
            logger.debug("静默窗口到期但 cycle 在跑，已注册完成后重试")
            return

        try:
            self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
        finally:
            self._exit_cycle()
    except Exception as e:
        logger.error(f"静默窗口兜底触发异常: {e}", exc_info=True)
```

#### 3.2.9 `Bot.on_group_recall` 改动

当前用 `self.history.append_recall_notice`。改为路由到 ctx：

```python
def on_group_recall(self, data: dict):
    try:
        recalled_msg_id = str(data.get("message_id", ""))
        operator_id = str(data.get("operator_id", ""))
        group_id = str(data.get("group_id", ""))
        if not recalled_msg_id:
            logger.warning(f"撤回通知缺少 message_id: {data}")
            return
        if self.config.napcat.group_id and group_id != self.config.napcat.group_id:
            return

        ctx = self.groups.get(group_id)
        if ctx is None:
            return

        logger.info(f"收到撤回通知: recalled_msg_id={recalled_msg_id} operator={operator_id}")
        ctx.history.append_recall_notice(recalled_msg_id, operator_id)
    except Exception as e:
        logger.error(f"处理撤回通知失败: {e}", exc_info=True)
```

#### 3.2.10 `Bot.on_group_poke` 改动

类似 on_group_recall，路由到 ctx：

```python
def on_group_poke(self, data: dict):
    try:
        target_id = str(data.get("target_id", ""))
        poker_id = str(data.get("user_id", ""))
        group_id = str(data.get("group_id", ""))
        if self.config.napcat.group_id and group_id != self.config.napcat.group_id:
            return
        if target_id != self.self_qq:
            logger.debug(f"忽略非戳自己的 poke: target={target_id} poker={poker_id}")
            return

        ctx = self.groups.get(group_id)
        if ctx is None:
            return

        poker_nick = self.napcat.get_nickname(poker_id)
        logger.info(f"收到戳一戳: poker={poker_nick}({poker_id})")
        ctx.history.append_poke_notice(poker_id, poker_nick)
        self._try_trigger_immediate(ctx, hard=True)
        self._reschedule_quiet_trigger(ctx)
    except Exception as e:
        logger.error(f"处理戳一戳失败: {e}", exc_info=True)
```

#### 3.2.11 `Bot.on_active_trigger` 改动

当前直接调 `_run_llm_cycle(is_active=True)`。改为指定群（阶段 A 单群，直接取 config 的 group_id）：

```python
def on_active_trigger(self):
    try:
        logger.info("主动触发：执行 LLM 调用")
        # 阶段 A：单群，直接取 config 的 group_id
        # 阶段 D：改为每群独立 scheduler，这里就不需要了
        group_id = self.config.napcat.group_id
        ctx = self.groups.get(group_id)
        if ctx is None:
            logger.warning(f"主动触发但未找到群 {group_id} 的上下文")
            return

        if not self._try_enter_cycle():
            ctx._cycle_pending = True
            logger.debug("主动触发但 cycle 在跑，已注册完成后重试")
            return

        try:
            self._run_llm_cycle(ctx, soft_factors=None, is_active=True)
        finally:
            self._exit_cycle()
    except Exception as e:
        logger.error(f"主动触发异常: {e}", exc_info=True)
```

#### 3.2.12 `Bot._run_llm_cycle` 改动

这是改动最多的方法。当前直接访问 `self.history`。改为接受 ctx 参数，所有 `self.history` → `ctx.history`，`self.attribution` → `ctx.attribution`，`self.last_reply_time` → `ctx.last_reply_time`。

关键改动点（逐行对照）：

```python
def _run_llm_cycle(self, ctx: GroupContext, soft_factors, is_active: bool):
    while True:
        # 1. drain fast_buffer
        drained = ctx.history.drain_buffer_to_pending()  # self.history -> ctx.history
        if drained > 0:
            logger.debug(f"drain {drained} 条消息到 pending")

        # 2. 延迟回复到期
        due_count = ctx.history.pop_due_delayed_into_pending()
        if due_count > 0:
            logger.info(f"延迟回复到期，{due_count} 条消息已加入 pending")

        # 3. pending 空检查
        if not ctx.history.pending_group_msgs and not is_active:
            logger.debug("pending 为空，跳过 LLM 调用")
            if self._consume_pending_trigger(ctx):  # 改这里
                continue
            return

        # 4. 构建 user_content
        summary = ctx.history.get_summary()
        system_prompt = self.persona_renderer.render_system_prompt(summary)
        history_messages = ctx.history.get_messages_for_llm()
        member_list = self._build_member_list(ctx)  # 改这里：传 ctx
        pending_text = ctx.history.build_user_content()
        new_user_content = self.persona_renderer.render_user_content(
            pending_text, member_list, self.self_nickname, self.self_qq,
            is_active=is_active,
        )

        # 5. 调用 LLM
        raw_result = self.llm.chat(system_prompt, history_messages, new_user_content)
        if raw_result is None:
            logger.warning("LLM 调用失败，本轮跳过")
            if self._consume_pending_trigger(ctx):  # 改这里
                continue
            return

        # 6. 解析 + 落地
        parsed = parse_and_validate(raw_result)
        logger.info(f"LLM 返回 action={parsed.action} ...")

        if parsed.reply_delay_minutes > 0 and parsed.action == "silent":
            ctx.history.stash_pending_as_delayed(parsed.reply_delay_minutes)
            if self._consume_pending_trigger(ctx):  # 改这里
                continue
            return

        ctx.history.consume_pending_into_user()
        ctx.history.append_turn(new_user_content, raw_result)

        # 7. 发送 + 归因
        self._handle_result(ctx, parsed, soft_factors)  # 改这里：传 ctx

        # 8. 检查重试
        soft_factors = None
        is_active = False
        if not self._consume_pending_trigger(ctx):  # 改这里
            return
        if time.time() - ctx.last_reply_time < SOFT_COOLDOWN_MIN:  # 改这里
            logger.debug("刚回完消息，新触发等静默窗口兜底")
            return
        logger.debug("上一轮 silent，立即重跑 cycle")
```

#### 3.2.13 `Bot._consume_pending_trigger` 改动

当前检查 `self._cycle_pending`。改为接受 ctx：

```python
def _consume_pending_trigger(self, ctx: GroupContext) -> bool:
    with self._cycle_lock:
        if ctx._cycle_pending:  # 改这里
            ctx._cycle_pending = False
            return True
        return False
```

> **注意**：`_cycle_lock` 仍保护这个检查（全局锁），因为 `_cycle_running` 是全局的。`ctx._cycle_pending` 虽然是 per-group 的，但它的读写都发生在 cycle 串行保护下（要么在 cycle 内部，要么在撞 cycle 时设置），所以用全局 `_cycle_lock` 保护是正确的。

#### 3.2.14 `Bot._handle_result` 改动

当前用 `self.history`、`self.attribution`、`self.config.napcat.group_id`。改为接受 ctx：

```python
def _handle_result(self, ctx: GroupContext, parsed, soft_factors):
    # 延迟（不变）
    if parsed.delay_seconds > 0 or parsed.messages:
        total_text_len = sum(len(_msg_to_text(m)) for m in parsed.messages)
        jitter = random.uniform(0.3, 1.2) * max(1, total_text_len // 5)
        total_delay = parsed.delay_seconds + min(jitter, 8.0)
        if total_delay > 0:
            logger.debug(f"延迟发送 {total_delay:.1f}s")
            time.sleep(total_delay)

    if parsed.action == "silent":
        if soft_factors is not None:
            ctx.attribution.update(soft_factors, "silent")  # 改这里
        self.affinity.apply_delta(parsed.affinity_delta)  # affinity 全局，不变
        return

    if parsed.action == "react":
        msg_id = ctx.history.get_msg_id_by_id(parsed.react_target_msg_id)  # 改这里
        if not msg_id:
            logger.warning(f"react 段 react_target_msg_id={parsed.react_target_msg_id} 无效")
        else:
            self.emoji_reactor.react(ctx.group_id, msg_id, parsed.react_emoji_id)  # 改这里：用 ctx.group_id
        if soft_factors is not None:
            ctx.attribution.update(soft_factors, "react")  # 改这里
        self.affinity.apply_delta(parsed.affinity_delta)
        return

    # reply / multi_reply
    self._send_messages(ctx, parsed.messages)  # 改这里：传 ctx
    ctx.last_reply_time = time.time()  # 改这里

    self.affinity.apply_delta(parsed.affinity_delta)

    if soft_factors is not None:
        ctx.attribution.update(soft_factors, parsed.action)  # 改这里
```

#### 3.2.15 `Bot._send_messages` 改动

当前用 `self.history` 和 `self.config.napcat.group_id`。改为接受 ctx：

```python
def _send_messages(self, ctx: GroupContext, messages: list):
    segments_list = self.message_sender.build_segments(messages, ctx.history)  # 改这里
    for i, segs in enumerate(segments_list):
        handled = False
        for seg in segs:
            if seg.get("type") == "forward":
                data = seg.get("data", {})
                self.napcat.send_group_forward_msg(data.get("messages", []), data.get("title", ""))
                handled = True
                break
            if seg.get("type") == "image":
                try:
                    self.image_sender.send(ctx.group_id, seg.get("data", {}))  # 改这里
                except NotImplementedError:
                    pass
                handled = True
                break
            if seg.get("type") == "voice":
                data = seg.get("data", {})
                channel = data.get("channel", "ai_record")
                if channel == "ai_record":
                    self.ai_voice_sender.send(ctx.group_id, data)  # 改这里
                elif channel == "local_file":
                    self.local_voice_sender.send(ctx.group_id, data)  # 改这里
                handled = True
                break
        if handled:
            self._append_bot_reply_to_buffer(ctx, messages[i])  # 改这里
            continue

        normal_segs = [s for s in segs if s.get("type") not in ("forward", "image", "voice")]
        if normal_segs:
            self.message_sender.send_group_message(ctx.group_id, normal_segs)  # 改这里

        self._append_bot_reply_to_buffer(ctx, messages[i])  # 改这里

        if i < len(segments_list) - 1:
            time.sleep(random.uniform(0.8, 2.5))
```

#### 3.2.16 `Bot._append_bot_reply_to_buffer` 改动

```python
def _append_bot_reply_to_buffer(self, ctx: GroupContext, msg):
    msg_text = _msg_to_text(msg)
    ctx.history.append_group_message(  # 改这里
        self.self_qq, self.self_nickname, msg_text, "", is_bot=True
    )
```

#### 3.2.17 `Bot._build_member_list` 改动

当前用 `self.history` 和 `self.napcat.member_cache` 和 `self.affinity`。改为接受 ctx：

```python
def _build_member_list(self, ctx: GroupContext) -> list:
    recent_speakers = ctx.history.get_recent_speakers()  # 改这里
    result = []
    for qq, info in self.napcat.member_cache.items():  # member_cache 全局，不变
        affinity = self.affinity.get(qq)  # affinity 全局，不变
        if qq not in recent_speakers and affinity <= 0 and qq != self.self_qq:
            continue
        result.append({
            "qq": qq,
            "nickname": info.get("card") or info.get("nickname") or qq,
            "role": info.get("role", "member"),
            "affinity": affinity,
        })
    return result
```

#### 3.2.18 `Bot.run` 改动

当前 `run` 启动 webhook 和 scheduler。`NapCatWebhookServer` 的 `target_group_id` 仍保留（阶段 A 单群过滤）。`scheduler.start(self.on_active_trigger)` 回调不变（on_active_trigger 内部自己找 ctx）。

**无需改动**。

### 3.3 不需要修改的文件

以下文件**本次不改**：

| 文件 | 原因 |
|---|---|
| `src/history.py` | 文件名参数化留给阶段 B；GroupContext 直接持有实例 |
| `src/attribution.py` | 同上 |
| `src/affinity.py` | 全局单例，留在 Bot |
| `src/trigger.py` | 构造方式不变，只是被 GroupContext 持有 |
| `src/napcat_client.py` | 单群不变，阶段 B 再改方法签名 |
| `src/persona.py` | 全局单例 |
| `src/scheduler.py` | 留在 Bot，阶段 D 移入 GroupContext |
| `src/parser.py` | 无状态 |
| `src/llm_client.py` | 无状态 |
| `src/senders/*` | 已接受 group_id 参数，无需改 |
| `src/config.py` | 单群配置不变 |
| `config.yaml` | 不变 |
| `src/background_compiler.py` | 不在主流程 |

---

## 四、验证方法

### 4.1 静态验证

1. **启动不报错**：`python main.py` 能正常启动，warmup 日志显示"预热完成"。
2. **文件加载**：`state/conversation.json`、`state/state.json`、`state/affinity.json`、`state/scheduler.json` 正常加载（日志无 warning）。
3. **无导入错误**：`from src.group_context import GroupContext` 成功。

### 4.2 行为验证（与重构前对比）

启动 bot 后在目标群内操作，观察以下行为与重构前一致：

1. **被动触发**：
   - 普通消息入 buffer，不立即触发 LLM（等静默窗口）。
   - @bot 立即触发（硬因子，5s 冷却）。
   - 兴趣关键词命中触发（软因子，10s 冷却）。
   - 静默窗口 150s 兜底触发。

2. **LLM 响应**：
   - LLM 返回 reply → 发送消息，fast_buffer 写入 bot 回复。
   - LLM 返回 silent → 不发送，归因更新。
   - LLM 返回 multi_reply → 逐条发送，间隔 0.8-2.5s。
   - LLM 返回 react → 调用 emoji 反应。

3. **历史压缩**：messages 达 120 轮时触发分层压缩（日志可见"分层压缩完成"）。

4. **主动触发**：scheduler 到期触发 LLM（is_active=True）。

5. **延迟回复**：LLM 输出 reply_delay_minutes → pending 存入 delayed_replies → 到期重新加入 pending。

6. **撤回通知**：撤回消息后 fast_buffer 追加伪消息，下一轮 LLM 可见。

7. **戳一戳**：戳 bot 后追加伪消息 + 硬触发。

8. **归因更新**：软因子触发后，silent/reply 都更新 dynamic_weight（冷启动期仅记日志）。

9. **亲密度更新**：LLM 输出 affinity_delta → affinity.json 更新。

### 4.3 日志关键词检查

重构后日志应出现：
- `预热完成，机器人 xxx(xxx)，群 xxx`（多了群号）
- `drain N 条消息到 pending`
- `LLM 返回 action=xxx`
- `消息评分=xxx hard=xxx`

不应出现：
- `未找到群 xxx 的上下文`（除非真的有非配置群的消息）
- 任何 traceback

---

## 五、不在本次范围内的事项

以下事项**阶段 A 不做**，留给后续阶段：

| 事项 | 阶段 |
|---|---|
| config.yaml 支持 `group_ids: [list]` | 阶段 B |
| HistoryManager/AttributionManager 文件名加 group_id 后缀 | 阶段 B |
| NapCatClient 方法接受 group_id 参数 | 阶段 B |
| NapCatClient.member_cache 嵌套化 `{group_id: {qq: info}}` | 阶段 B |
| webhook 去掉单群过滤、按 group_id 路由 | 阶段 B |
| `_cycle_queue` 全局队列（cycle 结束后扫描所有群 pending） | 阶段 B |
| 印记板（CrossGroupImpressionStore） | 阶段 C |
| system prompt 加"# 其他群的近况"节 | 阶段 C |
| scheduler 移入 GroupContext（每群独立倒计时） | 阶段 D |
| 群活跃度自适应 interval | 阶段 D 之后 |

---

## 六、风险与回滚

### 风险

1. **方法签名改动多**：`_run_llm_cycle`、`_handle_result`、`_send_messages` 等 10+ 个方法都要加 `ctx` 参数。漏改会导致 `self.history` 访问 AttributeError。
   - 缓解：改完后用 IDE 全局搜索 `self.history`、`self.attribution`、`self.trigger_evaluator`、`self.last_reply_time`、`self._quiet_timer`、`self._cycle_pending`，确认 Bot 类内不再有直接访问。

2. **`_cycle_pending` 语义变化**：从单 bool 变成 per-group bool。阶段 A 单群下行为等价，但要确认 while 循环里 `_consume_pending_trigger(ctx)` 的调用路径正确。
   - 缓解：仔细对照 `_run_llm_cycle` 的 3 个 `return` 点（pending 空、LLM 失败、延迟回复），确保每个都传 ctx。

3. **warmup 时序**：GroupContext 构造依赖 `self.self_qq`（warmup 后才有），所以 GroupContext 必须在 warmup 里创建，不能在 `__init__` 里。
   - 缓解：`__init__` 只初始化 `self.groups = {}`，warmup 填充。

### 回滚

如果重构后行为异常且难以定位，直接 `git revert` 即可。本次改动不涉及数据迁移（文件名不变），回滚后 state 文件直接可用。
