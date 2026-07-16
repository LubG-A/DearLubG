# 阶段 F：对话活跃度感知（direct 模式快速触发）

## 背景动机

当前架构在低频对话场景下表现迟钝。典型情境：

```
用户："Bot，你知道前端语言有哪些吗？"   → 提问语气硬触发 +60 → LLM 回复
Bot："HTML吧"
用户："还有呢？"                       → 评分不够，等 150s 兜底
```

"还有呢？"这类对话的中间轮次存在评分盲区：
- 不命中兴趣关键词（interests 里没有"前端"等通用词）
- 不是 @/提问语气（trigger_evaluator 基于关键词/模式，识别不出"还有呢"是追问）
- topic_hot 不触发（低频对话不算群消息量突增）
- friend_speak 仅在亲密度≥5 时给 +15；亲密度<5 时为 0 分
- 总分往往过不了 `peek_threshold=10`，只能等 `QUIET_WINDOW_SECONDS=150` 兜底

**真正卡住对话中间轮次的不是冷却，是评分根本不够触发"看一眼"**。`SOFT_COOLDOWN_MIN=10s` 在自然对话中不是瓶颈（用户打字回复很少落在 10s 内），瓶颈是评分门槛。

原 `topic_hot` 软因子解决的是"多人参与的热度场景"，与本场景正交。本阶段在不与 `topic_hot` 冲突的前提下，补齐对话的快速响应能力。方案不限于 1v1，Bot 参与的密集对话（含多人插话）都可进入 direct 状态。

## 核心设计决策

### F1：LLM 输出 `conversation_mode` 字段

在现有 JSON 输出协议新增字段：

```json
{
  "action": "reply",
  "conversation_mode": "direct" | "open",
  "targets": ["1305811703"],
  ...
}
```

- `direct`：Bot 刚回的是与某人的往返对话（被提问、被追问、对方在承接 Bot 的话）
- `open`：Bot 只是群内开放插话（吐槽、附和、旁观插话、主动开口）

**判断权归 LLM**：LLM 根据上下文语义判断自己刚回的是不是参与了一段对话，这是 LLM 擅长的语义判断而非规则匹配。

**字段缺失/非法值**：视为 `open`（保守降级，不影响现有逻辑）。

### F2：target 提取规则——以 `targets` 字段为准，多 target 并行

通过 LLM 输出的 `targets` 字段判断是否有明确对话对象：

| targets 字段 | 含义 | conversation_mode 处理 |
|---|---|---|
| `[]`（空 list） | 主动开口 / 无特定对象的插话 | **强制 open**（即使 LLM 输出 direct 也降级） |
| `["QQ或昵称", ...]` | 针对某人的回复 | 可进入 direct，**每个**元素都是一个独立 target |

target 解析：
- 元素为纯数字 → 视为 QQ 号
- 元素为非数字 → 在成员表反查昵称→QQ，反查失败则跳过该元素
- 所有元素都反查失败 → 强制 open

**多 target 设计**：`direct_targets` 是字典 `{qq: deadline}`，Bot 回复时为 targets 里的每个有效 QQ 设置/续命各自独立的窗口。这样：
- 1v1 场景：字典里只有一个人，行为同单 target
- 多人插话场景：Bot 回复 B 后，B 也加入字典；A 仍在字典里（窗口未过期）→ A 继续说话仍快速触发
- 各自独立衰减，互不影响

**不使用 reply/at 段作为 target 判断依据**：reply 段在引用回复时存在但不是判断 direct 的依据；at 段同理。判断 direct 的唯一依据是 `targets` 字段非空且可解析为有效 QQ。

### F3：direct 快速触发——绕过 peek_threshold

新增第四条触发路径，优先级介于硬因子与软因子之间：

| 路径 | 触发条件 | 归因 |
|---|---|---|
| 硬因子立即触发 | @/提问语气 + 过 HARD_COOLDOWN | 跳过 |
| **direct 快速触发（新增）** | **发送者在 ctx.direct_targets 字典中且未过该 target 的 deadline + 过 SOFT_COOLDOWN_MIN** | **跳过** |
| 软因子立即触发 | score ≥ peek_threshold + 过 SOFT_COOLDOWN_MIN | 生效 |
| 静默兜底 | 150s 无新消息 | 跳过 |

direct 快速触发的本质：**不经过评分门槛，直接让 LLM 看一眼**。LLM 仍然决定 silent/reply——如果 target 说"哈哈"，LLM 可以 silent；如果 target 说"还有呢？"，LLM 自然会回复。避免"每条都回"的 AI 感。

**归因跳过的理由**：direct 快速触发不是"某个软因子命中导致触发"，而是"LLM 上一轮自判处于活跃对话"，没有具体因子可归因。与硬触发、静默兜底一致。

### F4：活跃窗口基于真实群聊时间戳，各 target 独立

**窗口起点**：Bot 最后一条回复消息实际 append 到 fast_buffer 时记录的时间戳（`msg_timestamp`）。

**关键约束**：不使用 LLM 输出时间或代码处理时间作为窗口起点——因为发送侧可能耗时很长（绘图 20s+、媒体下载 30s+、multi_reply 逐条间隔），如果从代码处理开始算窗口，实际群聊窗口会被压缩到很小。

**窗口判断**：对 `direct_targets` 字典中的每个 target，`target_msg_timestamp - bot_last_msg_timestamp <= DIRECT_WINDOW_SECONDS`（默认 30s）时该 target 仍在窗口内。

**续命**：target 在窗口内每次发言，重置**该 target**的 `deadline = target_msg_timestamp + DIRECT_WINDOW_SECONDS`。不同 target 各自独立续命，互不影响。

**窗口长度**：`DIRECT_WINDOW_SECONDS=30`（可调整，写在 main.py 顶部常量）。

### F5：进入条件

同时满足：
1. LLM 输出 `action ∈ {reply, multi_reply}`（silent 不算，Bot 没在对话中）
2. LLM 输出 `conversation_mode=direct`
3. `targets` 字段非空且至少一个元素可解析为有效 QQ
4. Bot 实际发出了回复（发送失败的消息不 append 到 fast_buffer，不算发出）

满足后：
- 对 targets 里每个可解析的有效 QQ，设置 `ctx.direct_targets[qq] = bot_last_msg_timestamp + DIRECT_WINDOW_SECONDS`
- `ctx.conversation_mode = "direct"`
- 保留字典中其他未过期 target（不清空已有 target）

### F6：退出条件

**单 target 退出**（从字典移除该 target）：
1. **自然衰减**：该 target 30s 内无发言 → 该 target 的 deadline 过期 → 从 `direct_targets` 字典移除

**整体退出**（conversation_mode 回落 open，清空整个字典）：
2. **LLM 主动退出**：LLM 输出 `conversation_mode=open`（判断"话题已经聊完了"）
3. **字典清空**：所有 target 都自然衰减过期，字典为空 → conversation_mode 自动回落 open

**silent 不退出**：LLM 输出 `action=silent` 时，**保留** direct 状态和 direct_targets 字典。因为 target 可能说"哈哈"（LLM silent），紧接着说"对了还有呢？"——silent 后仍需保持快速触发能力。silent 只表示"这轮没必要回"，不表示"对话结束了"。

退出后清空 `ctx.direct_targets` 字典和 `ctx.conversation_mode`。

### F7：冷却保留

direct 快速触发仍受 `SOFT_COOLDOWN_MIN=10s` 约束：
- target 连发"还有呢""快说""人呢"时，10s 冷却挡住秒回，避免 Bot 显得急切
- 自然对话中用户打字回复很少落在 10s 内，10s 冷却不会卡住正常对话
- 冷却期内 target 继续发言 → 入 buffer，冷却到期后由 `_cycle_pending` 重跑（沿用现有撞 cycle 重试机制）

### F8：状态不持久化

`conversation_mode`、`direct_targets` 仅存于 GroupContext 内存，不写磁盘：
- 重启后最坏情况是当前对话的 direct 状态丢失，回落 open，不影响正确性
- 避免引入额外的持久化复杂度
- 与 `_cycle_pending` 等运行时状态一致（都不持久化）

### F9：多 target 无硬上限

`direct_targets` 字典不设数量上限：
- 靠 30s 自然衰减控制（不活跃的 target 自动移除）
- 靠 10s 冷却限制 Bot 回复频率（即使 5 个 target 都在发言，Bot 最多每 10s 回一次）
- 靠 LLM silent 决策控制回复密度（LLM 判断哪些值得回）
- 若实测发现过于积极，再加数量上限（如淘汰最久未活跃的）

## 改动范围

### LLM 侧（协议 + 提示词）

- **输出协议新增字段**：`conversation_mode: "direct" | "open"`
  - 默认/缺失/非法值视为 `open`
  - parser 解析时校验取值合法性，非法值降级为 `open`
- **提示词新增规则**（在"场景响应"章节后）：
  - `conversation_mode=direct`：仅当 Bot 刚回的是与某人的往返对话（被提问→回答、被追问→续答、对方在承接 Bot 的话）时使用
  - `conversation_mode=open`：开放插话（吐槽、附和、旁观插话、主动开口）时使用
  - `targets` 为空时强制 open（主动开口无特定对象）
  - direct 模式下若话题已结束（对方已收到答案、对话自然收尾），应输出 open 主动退出
  - silent 时 conversation_mode 无意义（保留上一轮状态），可不填或填上一轮值

### 代码侧

- **`src/group_context.py`**：GroupContext 新增两个字段
  - `conversation_mode: str = "open"`
  - `direct_targets: dict[str, float] = field(default_factory=dict)`（QQ → deadline 时间戳，秒）

- **`main.py`**：
  - 新增常量 `DIRECT_WINDOW_SECONDS = 30`
  - `_handle_result` 中：Bot 实际回复后，根据 LLM 输出的 `conversation_mode` 和 `targets` 字段，设置/重置 direct 状态（多 target 各自设置 deadline）
  - `on_group_message` 中：消息到达后，检查发送者是否在 `ctx.direct_targets` 字典中且未过该 target 的 deadline → 走 direct 快速触发路径，并重置该 target 的 deadline（续命）
  - `_try_trigger_immediate` 新增 `direct` 参数（类似 `hard` 参数），direct=True 时不传 soft_factors、归因跳过、仍受 SOFT_COOLDOWN_MIN 约束
  - 触发优先级：硬因子 > direct 快速触发 > 软因子 > 静默兜底
  - 定期清理 `direct_targets` 字典中过期的 target（可在 on_group_message 或 cycle 结束时顺手清理）

- **`src/parser.py`**：解析 `conversation_mode` 字段，校验取值，缺失/非法降级为 `open`

- **`src/persona.py`**：系统提示词新增 `conversation_mode` 字段说明与规则引导

### 不改的部分

- `trigger.py`：评分逻辑不变，direct 快速触发不经过评分
- `attribution.py`：归因逻辑不变，direct 快速触发跳过归因
- `history.py`：历史管理不变，conversation_mode 随 assistant 输出一起落地
- `scheduler.py`：主动触发逻辑不变
- 前缀缓存：`conversation_mode` 在 assistant 输出里，不进 system prompt，不破坏缓存

## 触发流程（修正后）

```
群消息到达（webhook，接收线程）
  ↓
main.py: Bot.on_group_message(msg)
  ├─ msg.group_id → 路由到 ctx
  ↓
ctx.history.append_group_message() → 入 ctx 的 fast_buffer（记录 msg_timestamp）
  ↓
触发决策四路（按优先级）：
  ├─ 硬因子（@/提问）且过 HARD_COOLDOWN → _try_trigger_immediate(ctx, hard=True)
  ├─ direct 快速触发：发送者 in ctx.direct_targets 且未过该 target 的 deadline 且过 SOFT_COOLDOWN_MIN
  │     → _try_trigger_immediate(ctx, direct=True)
  │     → 重置该 target 的 deadline = msg_timestamp + DIRECT_WINDOW_SECONDS（续命）
  ├─ 软因子 should_peek(score) 且过 SOFT_COOLDOWN_MIN → _try_trigger_immediate(ctx, soft_factors=...)
  └─ 低分/冷却中 → 入 buffer，重置 ctx 的静默窗口定时器
  ↓ [非阻塞检查 _cycle_running，撞则注册 _cycle_pending + 入全局 _cycle_queue]
_run_llm_cycle(ctx, soft_factors, is_active=False):
  ├─ drain_buffer_to_pending()
  ├─ ...（同现有流程）
  ├─ llm.chat()
  ├─ parser.parse_and_validate()  # 含 conversation_mode 解析
  ├─ consume_pending_into_user() + append_turn()
  ├─ _handle_result(ctx, soft_factors):
  │    ├─ 执行动作（silent/reply/multi_reply/react）
  │    ├─ [reply/multi_reply] _send_messages: 每条发送后 append 到 fast_buffer(is_bot=True)
  │    ├─ 更新 direct 状态：
  │    │    ├─ action=silent → 保留 direct_targets 字典不变（silent 不退出 direct）
  │    │    ├─ action=reply/multi_reply 且 conversation_mode=direct 且 targets 至少一个可解析
  │    │    │    → 对每个有效 target qq：direct_targets[qq] = bot_last_msg_timestamp + DIRECT_WINDOW_SECONDS
  │    │    │    → 保留字典中其他未过期 target（不清空已有）
  │    │    └─ action=reply/multi_reply 且（conversation_mode=open 或 targets 全部不可解析）
  │    │         → conversation_mode=open，清空 direct_targets 字典
  │    ├─ 顺手清理 direct_targets 中已过期的 target
  │    ├─ affinity.apply_delta()
  │    └─ attribution.update()  # direct=True 时跳过（无 soft_factors）
  └─ 检查 ctx._cycle_pending：有则重跑（不传 soft_factors），无则退出
```

## 与现有机制的关系

| 机制 | direct 模式下的行为 |
|---|---|
| `peek_threshold` | **绕过**（direct_targets 中的 target 发言直接进 LLM cycle） |
| `SOFT_COOLDOWN_MIN=10s` | **保留**（避免秒回，自然对话不受影响） |
| `QUIET_WINDOW_SECONDS=150s` | direct 模式下基本用不到（target 续说持续触发快速通道）；所有 target 停说 30s 后字典清空，回落 open，恢复 150s |
| `topic_hot` 软因子 | **正交不冲突**。topic_hot 描述"群热度→要不要看一眼"，conversation_mode 描述"Bot 是否在对话中→是否快速触发" |
| 硬触发（@/提问） | **独立有效**。target 在 direct 窗口内 @ 别人也走硬触发；别人 @ Bot 也走硬触发 |
| 归因系统 | **不扩展**。direct 快速触发跳过归因（无明确因子可归因） |
| 主动触发（ActiveScheduler） | **独立**。主动开口时 targets 为空 → 强制 open，不进入 direct |
| 前缀缓存 | **不受影响**。conversation_mode 在 assistant 输出里，不进 system prompt |

## 可配置项

`main.py` 顶部常量（非 config.yaml 配置项，与 `SOFT_COOLDOWN_MIN` 等一致）：

```python
DIRECT_WINDOW_SECONDS = 30   # direct 模式活跃窗口，target 在此窗口内发言触发快速触发
```

后续若需要调整，可移入 config.yaml。

## 验证标准

1. **追问场景**：用户提问→Bot 回复（conversation_mode=direct）→用户追问"还有呢？"→10s 内 direct 快速触发 LLM→Bot 续答
2. **target 续说续命**：target 在 30s 内连续发言，该 target 的 direct 状态保持，每次发言重置该 target 的窗口
3. **自然衰减**：某 target 30s 内无发言 → 该 target 从 direct_targets 字典移除；所有 target 都过期 → conversation_mode 回落 open
4. **LLM 主动退出**：LLM 输出 conversation_mode=open → 清空 direct_targets 字典
5. **silent 保留 direct**：LLM silent 时 direct_targets 字典保留，target 后续发言仍快速触发
6. **多 target 并行**：Bot 回复 A（direct）→ B 插话且 Bot 回复 B（direct）→ A 和 B 都在 direct_targets 字典里，各自独立窗口，各自续命
7. **主动开口不进入 direct**：主动触发时 targets 为空→强制 open
8. **targets 为空但 LLM 输出 direct**：强制降级为 open
9. **与 topic_hot 不冲突**：多人热闹场景下 topic_hot 正常触发，不被 direct 状态干扰
10. **归因系统不受污染**：direct 快速触发不更新 dynamic_weight
11. **前缀缓存命中率不下降**：conversation_mode 不进 system prompt

## 边界情况

- **Bot 回复发送失败**：消息不 append 到 fast_buffer，无 bot_last_msg_timestamp，不进入 direct 状态（即使 LLM 输出 direct）
- **targets 字段昵称反查失败**：该 target 跳过；所有 target 都反查失败 → 强制 open
- **direct 窗口内 target 发言但 LLM silent**：silent 保留 direct 状态，target 后续发言仍快速触发
- **target 在 direct 窗口内 @ 别人**：target 的这条消息既触发 direct 快速触发（因为发送者在 direct_targets 字典中），也正常评分（如果命中硬因子/软因子）——取优先级最高的路径（硬因子 > direct > 软因子）
- **multi_reply 逐条发送**：bot_last_msg_timestamp 取最后一条消息的 append 时间（确保窗口起点是 Bot 完整说完话的时间点）
- **direct 窗口内非 target 用户发言**：不触发 direct 快速触发，走正常评分流程（硬因子/软因子/兜底）。若 Bot 回复了该用户且 conversation_mode=direct，该用户加入 direct_targets 字典
- **所有 target 同时过期**：字典清空，conversation_mode 回落 open，下一条消息走正常评分流程
