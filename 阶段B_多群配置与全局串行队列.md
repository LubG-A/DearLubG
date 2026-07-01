# 阶段 B：多群配置 + 全局串行队列

**目标**：在阶段 A 的 GroupContext 结构上，实现真正的多群支持。多个群各自独立维护 conversation/attribution，共享一个 LLM 调用串行队列。
**约束**：现有单群数据需手动迁移到 per-group 子目录，迁移后单群行为与阶段 A 一致（主动触发除外，阶段 B 关闭）。
**验证标准**：配置两个群，两群同时活跃时 bot 能在两群间串行响应，不串台、不丢消息、不撞 rate limit。

---

## 一、背景与动机

### 阶段 A 留下的待办

阶段 A 完成了 GroupContext 结构抽取，但以下仍是"单群假象"：
1. `config.yaml` 只有 `group_id`（单群），`Bot.__init__` 只构造一个 GroupContext。
2. `NapCatClient` 在 `__init__` 存 `self.group_id`，所有 API 方法内部直接用，`member_cache` 是扁平 `{qq: info}`。
3. `NapCatWebhookServer` 用 `target_group_id` 过滤，只放行单群。
4. `HistoryManager` / `AttributionManager` 文件名固定（`conversation.json` / `state.json`），多群会互相覆盖。
5. `_cycle_pending` 虽然已 per-group，但没有"cycle 结束后扫描其他群"的机制——多群下群 B 撞 cycle 后无人处理。

### 阶段 B 的定位

阶段 B 解决上述全部问题，实现真正的多群：
- config 支持多群列表
- NapCatClient 多群化（方法接受 group_id，member_cache 嵌套化）
- webhook 按 group_id 路由
- per-group 文件存储（子目录方案，手动迁移）
- 全局 cycle 队列（cycle 结束后处理其他群的 pending）

**阶段 B 不做**：印记板（阶段 C）、主动触发多群化（阶段 D，阶段 B 关闭主动触发功能）。

---

## 二、设计决策与权衡

### 决策 B1：config.yaml 群配置格式

**采用方案 A（纯列表）**：废弃 `group_id`，改用 `group_ids: [list]`。

理由：
1. 兼容性包装会增加无意义开销——`group_id` 与 `group_ids` 的优先级、回退逻辑都是为一次性迁移服务的，迁移完成后即为死代码。
2. 当前不需要 per-group 配置（persona/affinity/trigger 参数都是全局的），对象列表方案暂无用武之地。
3. 最简单直接：`group_ids` 就是生效的群列表，无歧义。

config.yaml 示例（单群）：
```yaml
napcat:
  base_url: "http://127.0.0.1:8080"
  group_ids:
    - "945024095"
```

config.yaml 示例（多群）：
```yaml
napcat:
  base_url: "http://127.0.0.1:8080"
  group_ids:
    - "945024095"
    - "123456789"
```

### 决策 B2：per-group 文件存储结构

**方案 A（文件名后缀）**：`state/conversation_945024095.json` / `state/state_945024095.json`。
**方案 B（子目录）**：`state/945024095/conversation.json` / `state/945024095/state.json`。

**采用方案 B（子目录）**。理由：
1. 每群的 `conversation.json` / `state.json` / `attribution_log.jsonl` 集中在一个子目录，便于备份/清理/排查。
2. 文件名不变，`HistoryManager` / `AttributionManager` 代码零改动——只是传入的 `state_dir` 变为 `state/{group_id}`。
3. 方案 A 会让 `state/` 目录变拥挤（N 群 × 3 文件 = 3N 个文件平铺）。

目标结构（假设两个群）：
```
state/
├── 945024095/                    # per-group 子目录
│   ├── conversation.json
│   ├── state.json
│   └── attribution_log.jsonl
├── 123456789/                    # per-group 子目录
│   ├── conversation.json
│   ├── state.json
│   └── attribution_log.jsonl
├── affinity.json                 # 全局（跨群身份一致）
├── scheduler.json                # 全局（阶段 D 移入子目录）
├── background_story.md           # 全局（单一人格）
└── background_summary.json       # 全局
```

**数据迁移**：不编写迁移代码。实施前手动把 `state/` 根下的 `conversation.json` / `state.json` / `attribution_log.jsonl` 移到 `state/{group_id}/` 子目录（第一个群）即可——一次性操作不值得写代码。

### 决策 B3：NapCatClient 多群化

当前 `NapCatClient.__init__(base_url, group_id)` 存 `self.group_id`，方法内部直接用。`member_cache` 扁平 `{qq: info}`。

**采用**：
1. `__init__` 改为接受 `group_ids: list[str]`，存 `self.group_ids`。
2. `member_cache` 嵌套化：`{group_id: {qq: {nickname, card, role, title}}}`。
3. 所有 API 方法接受 `group_id` 参数：`send_group_msg(group_id, message)` / `get_group_member_list(group_id)` / `get_group_member_info(group_id, user_id)` / `send_group_ai_record(group_id, character, text)` / `set_msg_emoji_like(message_id, emoji_id, set_)`（此方法不需要 group_id，message_id 已定位消息）/ `get_ai_characters(group_id)` / `send_group_forward_msg(group_id, messages, title)`。
4. `warmup()` 为每个群拉取成员列表：`for gid in self.group_ids: self.member_cache[gid] = {...}`。
5. `get_nickname(group_id, qq)` 从对应群的 cache 取。

**为什么不新建多个 NapCatClient 实例**：NapCatClient 是无状态 HTTP 客户端（除了 member_cache 缓存），多个实例意味着多个 HTTP 连接池、多份 warmup 请求。单实例 + group_id 参数更高效。

### 决策 B4：webhook 多群路由

当前 `NapCatWebhookServer` 用 `target_group_id` 过滤，只放行单群。

**采用**：去掉 `target_group_id` 参数，按 `group_id` 路由：
- 消息/撤回/戳一戳的 `data.group_id` 在 `groups` 字典中 → 路由到对应 ctx。
- 不在 `groups` 中 → 丢弃（bot 未加入的群，或配置外的群）。

webhook 的 `on_message` / `on_recall` / `on_poke` 回调签名不变，路由逻辑在 `Bot` 的回调方法里完成（已有 `ctx = self.groups.get(group_id)` 逻辑，阶段 B 只需去掉防御性过滤的 `if group_id != self.config.napcat.group_id: return`）。

### 决策 B5：全局 cycle 队列

这是阶段 B 最核心的新增逻辑。阶段 A 的 `_cycle_pending` 已 per-group，但 cycle 结束后没有"扫描其他群"的机制。

**采用**：`_cycle_queue: deque[str]` + `_cycle_queue_set: set[str]`（去重）。

**入队时机**：`_try_enter_cycle(ctx)` 失败时（撞 `_cycle_running`），除了设 `ctx._cycle_pending = True`，还把 `ctx.group_id` 入队（如果不在队列里）。

**出队时机**：每个 cycle 结束后（`_exit_cycle` 之后），调 `_drain_cycle_queue()` 处理队列。

**`_drain_cycle_queue` 逻辑**（迭代，非递归，避免深递归）：
```python
def _drain_cycle_queue(self):
    while self._cycle_queue:
        group_id = self._cycle_queue.popleft()
        self._cycle_queue_set.discard(group_id)
        ctx = self.groups.get(group_id)
        if ctx is None or not ctx._cycle_pending:
            continue  # 已被消费或不存在
        # 进入 cycle（_cycle_running 已 False，但防御性检查）
        with self._cycle_lock:
            if self._cycle_running:
                self._cycle_queue.appendleft(group_id)  # 重新入队队首
                self._cycle_queue_set.add(group_id)
                return
            self._cycle_running = True
        try:
            self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
        finally:
            self._exit_cycle()
            # while 循环继续处理下一个群
```

**关键语义**：
- `_run_llm_cycle` 内部的 while 循环只处理**同群**的 pending（`_consume_pending_trigger(ctx)`）。
- `_drain_cycle_queue` 处理**跨群**的 pending（其他群在 cycle 期间撞上后入队的）。
- 重跑的 cycle 不传 `soft_factors`（归因跳过），`is_active=False`——与阶段 A 单群重试语义一致。
- `_cycle_queue` 的读写都在 `_cycle_lock` 保护下（入队在 `_try_enter_cycle` 内，出队在 `_drain_cycle_queue` 内），线程安全。

**为什么不扫描所有群**：遍历所有群检查 `_cycle_pending` 是 O(N)，且大部分群可能没有 pending。显式队列是 O(1) 入队 + O(M) 出队（M = 实际撞 cycle 的群数），更高效。

**set 去重的必要性**：实际运行中同一群确实会重复入队。场景：群 A 在 cycle 中（LLM 调用耗时数秒），群 B 连续收到多次 @bot（每次间隔 >5s 冷却），每次 `_try_trigger_immediate(B)` → `_try_enter_cycle(B)` 失败 → 尝试入队。无 set 时队列变为 `[B, B, B, ...]`，`_drain_cycle_queue` 处理首个 B 后消费 `_cycle_pending`，后续 B 因 pending=False 跳过——**正确但浪费**。set 以 2 行代码代价避免队列膨胀，推荐保留。极端情况（10 群同时活跃 × 每群 5 次触发）无 set 队列可达 50 条，有 set 仅 10 条。

### 决策 B6：on_active_trigger 阶段 B 策略

阶段 B 不实现多群主动触发。阶段 D 会把 scheduler 移入 GroupContext，实现每群独立倒计时——届时选群逻辑自然消失。

**采用**：阶段 B 关闭主动触发功能（`config.yaml` 中 `active_trigger.enabled: false`）。代码层面 `on_active_trigger` 仍保留，仅把 `self.config.napcat.group_id` 引用改为 `self.config.napcat.group_ids[0]`（防止 config 字段删除后引用报错），但不主动调用。

理由：
1. 明知后续要删除的过渡代码不值得编写——轮询/选群逻辑在阶段 D 会被完全替换，现在实现是浪费。
2. 关闭主动触发对核心功能（被动触发）零影响，bot 仍能正常响应群消息。
3. 阶段 D 实现每群独立倒计时后，直接开启 `enabled: true` 即可。

### 决策 B7：NapCatClient 方法签名改动策略

NapCatClient 的所有群相关方法都要加 `group_id` 参数。调用方（main.py / senders）需要逐个传 `ctx.group_id`。

**采用**：直接改签名，不做兼容层。理由：
1. NapCatClient 是内部封装，没有外部调用者。
2. 兼容层（如 `group_id=None` 时用 `self.group_id`）会引入"哪个 group_id 生效"的歧义，且 NapCatClient 不应该有"默认群"概念。
3. 阶段 B 一次性改完所有调用点，比渐进式改动更清晰。

---

## 三、具体改动清单

### 3.1 修改 `src/config.py`

#### 3.1.1 `NapCatConfig` 用 `group_ids` 替换 `group_id`

```python
@dataclass
class NapCatConfig:
    base_url: str
    group_ids: list = field(default_factory=list)  # 群列表（至少一个）
```

#### 3.1.2 `load_config` 解析 `group_ids`

```python
napcat_raw = raw["napcat"]
napcat = NapCatConfig(
    base_url=napcat_raw["base_url"],
    group_ids=napcat_raw.get("group_ids", []),
)
```

无需 `get_group_ids()` 辅助方法——`config.napcat.group_ids` 即是生效列表。

### 3.2 修改 `src/napcat_client.py`

#### 3.2.1 `NapCatClient.__init__` 改为接受 `group_ids`

```python
def __init__(self, base_url: str, group_ids: list[str]):
    self.base_url = base_url.rstrip("/")
    self.group_ids = [str(g) for g in group_ids]
    self.self_info: dict = {}
    # member_cache 嵌套化：{group_id: {qq: {nickname, card, role, title}}}
    self.member_cache: dict[str, dict] = {gid: {} for gid in self.group_ids}
```

#### 3.2.2 `warmup` 为每个群拉取成员

```python
def warmup(self):
    self.self_info = self.get_login_info()
    logger.info(f"机器人身份：{self.self_info.get('nickname')}({self.self_info.get('user_id')})")
    for gid in self.group_ids:
        members = self.get_group_member_list(gid)
        for m in members:
            qq = str(m.get("user_id"))
            detail = self.get_group_member_info(gid, qq)
            self.member_cache[gid][qq] = {
                "nickname": detail.get("nickname", ""),
                "card": detail.get("card", ""),
                "role": detail.get("role", "member"),
                "title": detail.get("title", ""),
            }
        logger.info(f"群 {gid} 成员缓存：{len(self.member_cache[gid])} 人")
```

#### 3.2.3 `get_nickname` 接受 `group_id`

```python
def get_nickname(self, group_id: str, qq: str) -> str:
    info = self.member_cache.get(group_id, {}).get(qq, {})
    return info.get("card") or info.get("nickname") or qq
```

#### 3.2.4 所有 API 方法接受 `group_id` 参数

逐个方法改签名（参数从无到有，内部 `self.group_id` → `group_id`）：
- `get_group_member_list(group_id)`
- `get_group_member_info(group_id, user_id)`
- `send_group_msg(group_id, message)`
- `send_group_ai_record(group_id, character, text)`
- `get_ai_characters(group_id)`
- `send_group_forward_msg(group_id, messages, title)`
- `fetch_ptt_text(message_id, quiet)` — **不变**（此方法不涉及 group_id，message_id 已定位消息）
- `set_msg_emoji_like(message_id, emoji_id, set_)` — **不变**（同上）
- `get_login_info()` — **不变**

#### 3.2.5 `NapCatWebhookServer` 去掉 `target_group_id`

```python
def __init__(self, host: str, port: int, on_message: Callable[[dict], None],
             on_recall: Optional[Callable[[dict], None]] = None,
             on_poke: Optional[Callable[[dict], None]] = None):
    # 删除 target_group_id 参数
    ...
```

`do_POST` 内部去掉 `if target_group_id and msg_group_id != target_group_id: 丢弃` 逻辑，直接异步调用回调。群过滤由 `Bot` 的回调方法里 `ctx = self.groups.get(group_id); if ctx is None: return` 完成。

### 3.3 修改 `src/group_context.py`

#### 3.3.1 `__init__` 传入 per-group `state_dir`

```python
def __init__(self, group_id, config, napcat, llm, affinity, self_qq, persona_name,
             state_dir="state"):
    self.group_id = group_id
    ...
    # per-group 子目录：state/{group_id}
    per_group_state_dir = f"{state_dir}/{group_id}"
    self.history = HistoryManager(config.trigger, state_dir=per_group_state_dir)
    self.history.set_summarizer(llm.summarize)
    self.attribution = AttributionManager(config, state_dir=per_group_state_dir)
    ...
```

`HistoryManager` 和 `AttributionManager` 代码**零改动**——它们已经接受 `state_dir` 参数，只是传入的值变了。

### 3.4 修改 `main.py`

#### 3.4.1 `__init__` 改动

```python
# NapCatClient 改为接受 group_ids
self.napcat = NapCatClient(self.config.napcat.base_url, self.config.napcat.group_ids)
...
# 新增 cycle 队列
from collections import deque
self._cycle_queue: deque[str] = deque()
self._cycle_queue_set: set[str] = set()
```

#### 3.4.2 `warmup` 改为为每个群创建 GroupContext

```python
def warmup(self):
    self.napcat.warmup()
    self.self_qq = str(self.napcat.self_info.get("user_id", ""))
    self.self_nickname = self.napcat.self_info.get("nickname", "")

    for group_id in self.config.napcat.group_ids:
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
    logger.info(f"预热完成，机器人 {self.self_nickname}({self.self_qq})，"
                f"群 {list(self.groups.keys())}")
```

#### 3.4.3 `_probe_ai_voice_character` 改动

`get_ai_characters` 现在接受 `group_id`。用第一个群探测即可（character 是全局的）：

```python
def _probe_ai_voice_character(self):
    character = self.config.voice.ai_record_character
    group_id = self.config.napcat.group_ids[0]  # 用第一个群探测
    try:
        characters = self.napcat.get_ai_characters(group_id)
        ...
```

#### 3.4.4 `on_group_message` 改动

去掉 `if msg_group_id and msg_group_id != self.config.napcat.group_id: return` 防御性过滤（webhook 已不再过滤，由 `ctx is None` 兜底）。`get_nickname` 加 `group_id` 参数：

```python
sender_nick = self.napcat.get_nickname(msg_group_id, sender_qq)
```

#### 3.4.5 `on_group_recall` / `on_group_poke` 改动

同样去掉防御性过滤。`on_group_poke` 里 `get_nickname` 加 `group_id`：

```python
poker_nick = self.napcat.get_nickname(group_id, poker_id)
```

#### 3.4.6 `_transcribe_voice_async` 改动

`fetch_ptt_text` 签名不变（不需要 group_id）。无改动。

#### 3.4.7 `_try_enter_cycle` 改动（核心）

撞 cycle 时入队：

```python
def _try_enter_cycle(self, ctx: GroupContext) -> bool:
    with self._cycle_lock:
        if self._cycle_running:
            ctx._cycle_pending = True
            self._enqueue_group(ctx.group_id)  # 新增：入队
            return False
        self._cycle_running = True
        return True

def _enqueue_group(self, group_id: str):
    """群入队 cycle 队列（去重）。在 _cycle_lock 保护下调用。"""
    if group_id not in self._cycle_queue_set:
        self._cycle_queue.append(group_id)
        self._cycle_queue_set.add(group_id)
```

#### 3.4.8 新增 `_drain_cycle_queue` 方法

```python
def _drain_cycle_queue(self):
    """处理 cycle 队列：cycle 结束后处理其他撞 cycle 的群。
    
    迭代处理（非递归），避免多群排队时递归过深。
    每次从队首取一个群，执行 _run_llm_cycle，结束后继续取下一个。
    """
    while self._cycle_queue:
        group_id = self._cycle_queue.popleft()
        self._cycle_queue_set.discard(group_id)
        ctx = self.groups.get(group_id)
        if ctx is None or not ctx._cycle_pending:
            continue  # 已被消费或不存在
        
        with self._cycle_lock:
            if self._cycle_running:
                # 不应该发生（_cycle_running 刚清），防御性重新入队
                self._cycle_queue.appendleft(group_id)
                self._cycle_queue_set.add(group_id)
                return
            self._cycle_running = True
        
        try:
            logger.debug(f"从队列处理群 {group_id} 的 pending cycle")
            self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
        finally:
            self._exit_cycle()
            # while 循环继续处理下一个群
```

#### 3.4.9 `_try_trigger_immediate` / `_on_quiet_timeout` / `on_active_trigger` 改动

这三个方法的 `try/finally` 块里，`_exit_cycle()` 之后新增 `_drain_cycle_queue()` 调用：

```python
def _try_trigger_immediate(self, ctx, hard=False, soft_factors=None):
    ...
    if not self._try_enter_cycle(ctx):
        return  # 已入队
    try:
        self._run_llm_cycle(ctx, soft_factors=soft_factors, is_active=False)
    finally:
        self._exit_cycle()
        self._drain_cycle_queue()  # 新增
```

`_on_quiet_timeout` 同理。`on_active_trigger` 见下条。

#### 3.4.10 `on_active_trigger` 最小改动

阶段 B 关闭主动触发（`config.yaml` 中 `active_trigger.enabled: false`），但代码仍保留正确引用。只把 `self.config.napcat.group_id` 改为 `self.config.napcat.group_ids[0]`，避免字段删除后报错。无轮询逻辑。

```python
def on_active_trigger(self):
    try:
        logger.info("主动触发：执行 LLM 调用")
        group_id = self.config.napcat.group_ids[0]  # 阶段 B 仅引用第一个群，阶段 D 改为每群独立
        ctx = self.groups.get(group_id)
        if ctx is None:
            logger.warning(f"主动触发但未找到群 {group_id} 的上下文")
            return

        if not self._try_enter_cycle(ctx):
            return  # 已入队
        try:
            self._run_llm_cycle(ctx, soft_factors=None, is_active=True)
        finally:
            self._exit_cycle()
            self._drain_cycle_queue()
    except Exception as e:
        logger.error(f"主动触发异常: {e}", exc_info=True)
```

#### 3.4.11 `_send_messages` 改动

`send_group_forward_msg` 加 `ctx.group_id`：

```python
self.napcat.send_group_forward_msg(ctx.group_id, data.get("messages", []), data.get("title", ""))
```

其余 sender 调用已经在阶段 A 改为 `ctx.group_id`，无需再改。

#### 3.4.12 `_build_member_list` 改动

`member_cache` 嵌套化后，从对应群的 cache 取：

```python
def _build_member_list(self, ctx: GroupContext) -> list:
    recent_speakers = ctx.history.get_recent_speakers()
    group_members = self.napcat.member_cache.get(ctx.group_id, {})
    result = []
    for qq, info in group_members.items():
        affinity = self.affinity.get(qq)
        if qq not in recent_speakers and affinity <= 0 and qq != self.self_qq:
            continue
        result.append({...})
    return result
```

#### 3.4.13 `run` 改动

`NapCatWebhookServer` 去掉 `target_group_id` 参数：

```python
server = NapCatWebhookServer(
    webhook_host, webhook_port, self.on_group_message,
    on_recall=self.on_group_recall,
    on_poke=self.on_group_poke,
)
...
logger.info(f"机器人启动，监听群 {list(self.groups.keys())}")
```

### 3.5 不需要修改的文件

| 文件 | 原因 |
|---|---|
| `src/history.py` | 已接受 `state_dir` 参数，传入子目录即可 |
| `src/attribution.py` | 同上 |
| `src/affinity.py` | 全局单例，仍用默认 `state` 目录 |
| `src/trigger.py` | 无 group_id 概念，通过 `ctx.history` 访问 |
| `src/persona.py` | 全局单例 |
| `src/scheduler.py` | 留在 Bot，阶段 D 移入 GroupContext |
| `src/parser.py` / `src/llm_client.py` | 无状态 |
| `src/senders/*` | 已接受 `group_id` 参数，调用方传 `ctx.group_id` 即可 |
| `src/background_compiler.py` | 全局，不在主流程 |

---

## 四、验证方法

### 4.1 数据迁移（手动）

实施前手动把 `state/` 根下的 `conversation.json` / `state.json` / `attribution_log.jsonl` 移到 `state/{group_id}/` 子目录（第一个群）。不编写迁移代码。

### 4.2 单群兼容验证

config.yaml 填 `group_ids: ["945024095"]`（单元素列表），启动后行为与阶段 A 完全一致：
- 一个 GroupContext 被创建。
- webhook 路由到该群。
- 被动触发、延迟回复、撤回、戳一戳全部正常。
- 主动触发已关闭（`active_trigger.enabled: false`）。

### 4.3 多群验证

config.yaml 填 `group_ids: [A, B]`，启动后：
1. 两个 GroupContext 被创建，各自的 `state/A/` / `state/B/` 子目录存在。
2. 群 A 消息 → 入 A 的 fast_buffer → A 的触发/评分/cycle。
3. 群 B 消息 → 入 B 的 fast_buffer → B 的触发/评分/cycle。
4. 群 A cycle 期间群 B 触发 → B 入队 → A cycle 结束后 `_drain_cycle_queue` 处理 B。
5. 两群的 conversation 独立（A 的消息不进 B 的 history，反之亦然）。
6. 两群的归因参数独立（A 的 silent 不影响 B 的 dynamic_weight）。
7. 亲密度全局共享（同一 QQ 在 A 群的互动影响 B 群的亲密度）。

### 4.4 日志关键词检查

- `预热完成，机器人 xxx(xxx)，群 ['945024095', '123456789']`
- `群 945024095 成员缓存：N 人` / `群 123456789 成员缓存：M 人`
- `从队列处理群 123456789 的 pending cycle`（多群撞 cycle 时）
- `机器人启动，监听群 ['945024095', '123456789']`

不应出现：
- `未找到群 xxx 的上下文`（除非真有配置外的群消息）
- 任何 traceback
- 两群消息串台（A 的消息出现在 B 的 conversation.json）

---

## 五、不在本次范围内的事项

| 事项 | 阶段 |
|---|---|
| 印记板（CrossGroupImpressionStore） | 阶段 C |
| system prompt 加"# 其他群的近况"节 | 阶段 C |
| 印记惰性更新（每群 trigger_count + TTL） | 阶段 C |
| 主动触发：阶段 B 关闭，scheduler 移入 GroupContext 每群独立倒计时 | 阶段 D |
| 群活跃度自适应 interval | 阶段 D 之后 |
| per-group persona / trigger 参数 | 未来（当前全局够用） |

---

## 六、风险与回滚

### 风险

1. **NapCatClient 签名改动波及面广**：所有 API 方法都改签名，调用方漏改会导致 `TypeError`。
   - 缓解：改完后用 IDE 全局搜索 `self.napcat.` 确认所有调用点都传了 `group_id`（`fetch_ptt_text` / `set_msg_emoji_like` / `get_login_info` 除外）。

2. **cycle 队列死锁/活锁**：如果 `_drain_cycle_queue` 的 while 循环里 `_run_llm_cycle` 又入队了新群，可能无限循环。
   - 分析：不会。`_run_llm_cycle` 内部不调 `_try_enter_cycle`（它已在 cycle 里），所以不会入队。入队只发生在外部触发（接收线程的 `_try_trigger_immediate` / `_on_quiet_timeout` / `on_active_trigger`），这些是异步的，不会阻塞 `_drain_cycle_queue`。
   - 缓解：`_drain_cycle_queue` 的 while 循环里每次 `_run_llm_cycle` 都是有限的（有 pending 空检查退出），不会无限。

3. **member_cache 嵌套化后 _build_member_list 取错群**：如果 `ctx.group_id` 传错，会取到错的成员列表。
   - 缓解：`_build_member_list(ctx)` 在阶段 A 已接受 ctx，阶段 B 只改 `self.napcat.member_cache` → `self.napcat.member_cache.get(ctx.group_id, {})`。

### 回滚

阶段 B 回滚：
1. `git revert` 代码改动。
2. 手动把 `state/{group_id}/` 下的文件移回 `state/` 根。
3. config.yaml 改回 `group_id` 格式。

建议在实施前备份 `state/` 目录。
