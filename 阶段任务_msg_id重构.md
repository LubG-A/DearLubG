# 阶段任务：msg_id 重构 + 撤回消息支持

## 背景与目标

当前引用机制基于"正序编号 index"（`[1] [2] [3]...`），存在两大局限：
1. **跨轮引用失效**：`_rendered_pending_snapshot` 只存本轮 pending，LLM 无法引用历史 user_content 里的消息（index 跨轮不唯一）。
2. **撤回消息无法定位**：webhook 的 `group_recall` 通知只带 `msg_id`，无 index 编号，bot 无法知道撤回的是历史里的哪条消息。

**目标**：废弃 index 机制，统一用 `msg_id` 作为消息引用标识；并新增撤回消息通知功能，让 bot 能理解"哪条消息被撤回了"。

## 渲染格式（最终确认）

- 可引用消息（群成员）：`[#1281341473] [20:03:37] 张三(123456789): 你好`
- 不可引用消息（bot 自身）：`[20:03:37] [bot]林夏(789): 嗯`（无 `[#]` 前缀）
- 撤回通知（伪消息）：`[20:03:37] [系统] (撤回通知): msg_id=1281341473 的消息被撤回`

对话顺序由消息在 user_content 中的物理排列表达，与真人看群聊一致，不再保留正序编号 N。

---

## 编号机制（index）使用点全景（调研结果）

### 1. history.py
- L67: `_rendered_pending_snapshot` 字段（存本轮 pending 浅拷贝，用于 index→msg_id 映射）
- L172-198: `build_user_content` 渲染 `[N] [time] ...` 格式，`N = i+1`
- L188-189: 快照创建 `self._rendered_pending_snapshot = list(self.pending_group_msgs)`
- L302-319: `get_msg_id_by_index(index)` 按正序编号取 msg_id，bot 消息返回 None

### 2. persona.py
- 规则 8（引用回复章节）：教 LLM 用 `target_msg_index: N`，提到 `[1][2][3]` 编号
- 输出协议 JSON 示例：`{"type": "reply", "data": {"target_msg_index": 3}}`、`react_target_msg_index: 1`
- user content 模板说明：`[N] 编号可用于 reply 段引用`

### 3. message_sender.py
- L59-67: reply 段解析 `target_msg_index` → `history.get_msg_id_by_index(target_idx)` → 构造 reply 段

### 4. main.py
- L348: 注释提到快照供 reply/react 引用
- L454: react 段 `self.history.get_msg_id_by_index(parsed.react_target_msg_index)`

### 5. parser.py
- L25: `ParsedResult.react_target_msg_index: int = 0`
- L95-109: reply 段校验 `target_msg_index` 为正整数
- L140-155: `react_target_msg_index` 校验为正整数

### 6. 摘要系统
- llm_client.py L28-36: `_SUMMARY_SYSTEM_PROMPT` 摘要 prompt，未提及保留 msg_id
- llm_client.py L108-115: `summarize` 取 user_content 后 300 字拼成文本（含 `[N]` 编号）
- history.py L289-296: 远期朴素摘要取 user_content 后 100 字（含 `[N]` 编号）

### 7. 撤回消息（当前空白）
- napcat_client.py L219: webhook 只分发 `post_type=="message"`，notice 被丢弃
- 无任何撤回处理逻辑

---

## 一、需要修改的内容

> 严格按此顺序执行，每个大步骤完成后重新阅读代码评估下一步。

### 步骤 1：history.py — build_user_content 渲染格式

**改动**：
- 渲染格式从 `[N] [time] nickname(qq): content` 改为：
  - 群成员消息（有 msg_id）：`[#1281341473] [20:03:37] 张三(123456789): 你好`
  - bot 消息（无 msg_id）：`[20:03:37] [bot]林夏(789): 嗯`（不带 `[#]` 前缀，表示不可引用）
- 删除 `seq = i + 1` 编号逻辑
- 快照机制保留（用途从 index→msg_id 映射，改为 msg_id 有效性校验 + bot 消息识别）

**冲突评估**：
- `update_group_message_content` 按 msg_id 查找 entry，不依赖 index，无冲突
- `recent_message_count` 按时间戳统计，不依赖 index，无冲突
- 摘要压缩的远期朴素摘要取 user_content 后 100 字，格式变化不影响逻辑（文本截断仍有效）

### 步骤 2：history.py — get_msg_id_by_index → get_msg_id_by_id + 删除快照机制

**改动**：
- 新增 `get_msg_id_by_id(target_msg_id: str) -> Optional[str]`：
  - 空串 → 返回 None（无效）
  - 非空 → 直接返回该 msg_id（不做本地校验，让 NapCat 做最终校验）
  - 理由：新方案下 msg_id 由 LLM 直接输出，无需 index→msg_id 反查映射；本地校验价值有限（防不住近似错误），跨轮引用本就无法本地校验
- **删除快照机制**（新方案下已无存在理由）：
  - 删除字段 `self._rendered_pending_snapshot`（L67）
  - 删除 `build_user_content` 中的 `self._rendered_pending_snapshot = list(...)` 赋值（L189）
  - 删除 `get_msg_id_by_index` 方法（L302-319，被新方法取代）
  - 清理相关注释（L64-66、L175-176）

**冲突评估**：
- 调用方（message_sender L61 / main.py L454）同步改为 `get_msg_id_by_id`，无冲突
- 快照删除后 `build_user_content` 不再创建浅拷贝，逻辑更简单
- 无其他代码依赖快照（已扫描确认，仅 5 处代码引用，全部在本步骤处理）

### 步骤 3：parser.py — 字段与校验

**改动**：
- `ParsedResult.react_target_msg_index: int = 0` → `react_target_msg_id: str = ""`
- reply 段校验（L95-109）：
  - `target_msg_index` int 校验 → `target_msg_id` str 非空校验
  - 空串或非 str → 丢弃该 reply 段，记 WARNING
  - 写回 `data_dict["target_msg_id"] = str(target_msg_id)`
- react 校验（L140-155）：
  - `react_target_msg_index` int 校验 → `react_target_msg_id` str 非空校验
  - 空串 → 默认空串（sender 会跳过）

**冲突评估**：
- ParsedResult 字段改名，调用方（main.py L454）同步改，无冲突

### 步骤 4：message_sender.py — reply 段解析

**改动**：
- L59-67 reply 段：
  - `target_idx = data.get("target_msg_index", 0)` → `target_msg_id = data.get("target_msg_id", "")`
  - `history.get_msg_id_by_index(target_idx)` → `history.get_msg_id_by_id(target_msg_id)`
  - msg_id 无效（返回 None）→ 记 WARNING，跳过 reply 段，保留 text 附文（如果有）
  - msg_id 有效 → 构造 `{"type": "reply", "data": {"id": msg_id}}`

**冲突评估**：
- 与步骤 2 的新方法配套，无冲突

### 步骤 5：main.py — react 段解析

**改动**：
- L454: `parsed.react_target_msg_index` → `parsed.react_target_msg_id`
- `self.history.get_msg_id_by_index(...)` → `self.history.get_msg_id_by_id(parsed.react_target_msg_id)`
- react 为预留接口，msg_id 无效时记 WARNING 并跳过

**冲突评估**：
- 与步骤 3 字段改名配套，无冲突

### 步骤 6：persona.py — 规则 8 + 输出协议

**改动**：
- **规则 8（引用回复）重写**：
  - 说明消息行首带 `[#msg_id]` 标记（bot 消息无此标记，不可引用）
  - 引用时复制消息行首的 msg_id：`{"type": "reply", "data": {"target_msg_id": "1281341473", "text": "附文"}}`
  - 适用场景描述保留（非最后一条、时间间隔长、刷屏等）
  - 强调：引用时必须精确复制 msg_id 数字，不可近似
- **输出协议 JSON 示例**：
  - `{"type": "reply", "data": {"target_msg_index": 3}}` → `{"type": "reply", "data": {"target_msg_id": "1281341473"}}`
  - `"react_target_msg_index": 1` → `"react_target_msg_id": "1281341473"`
- **user content 模板说明**：
  - `[N] 编号可用于 reply 段引用` → `[#msg_id] 标记可用于 reply 段引用，bot 消息无此标记不可引用`

**冲突评估**：
- persona 改动不影响代码逻辑，仅影响 LLM 输出格式，与 parser 改动配套

### 步骤 7：llm_client.py — 摘要 prompt 引导保留 msg_id

**改动**：
- `_SUMMARY_SYSTEM_PROMPT` 增加一条要求：
  - "保留消息的 `[#msg_id]` 标记（如有），便于后续引用和撤回定位"
- 理由：摘要后的 user_content 文本会进入 summary，若 LLM 摘要时丢弃 msg_id 标记，撤回历史消息时 bot 无法在摘要里定位。

**冲突评估**：
- 仅 prompt 文本调整，不影响 summarize 代码逻辑

### 步骤 8：napcat_client.py — webhook 分发 notice

**改动**：
- do_POST 的 post_type 分发（L219）增加分支：
  ```
  elif post_type == "notice" and data.get("notice_type") == "group_recall":
      Thread(target=on_recall, args=(data,), daemon=True).start()
  ```
- `on_recall` 回调需由 main.py 注入（同 on_message 模式）
- 群过滤：撤回通知也需检查 group_id 是否为目标群

**冲突评估**：
- 新增分支不影响现有 message 分发逻辑
- on_recall 回调注入模式与 on_message 一致，无冲突

### 步骤 9：main.py — 撤回处理逻辑

**改动**：
- 新增 `on_group_recall(data: dict)` 方法：
  - 提取 `message_id`、`operator_id`、`group_id`
  - 群过滤
  - 调用 `self.history.append_recall_notice(msg_id, operator_id)`
- warmup 时把 `on_recall` 回调注入 NapCatWebhookServer

**冲突评估**：
- 撤回处理异步执行，与消息接收线程独立
- 追加伪消息到 fast_buffer 复用 `append_group_message`，但需标记为系统消息（is_bot=False, msg_id=""）
- 与 LLM 工作线程的 drain 操作通过 `_buffer_lock` 保护，无竞态

### 步骤 10：history.py — 追加撤回通知伪消息

**改动**：
- 新增 `append_recall_notice(recalled_msg_id: str, operator_qq: str)` 方法：
  - 构造 entry：`{nickname: "系统", content: f"msg_id={recalled_msg_id} 的消息被撤回", msg_id: "", is_bot: False, is_recall_notice: True}`
  - append 到 fast_buffer（持 `_buffer_lock`）
  - 下一轮 drain 时自然进入 pending，LLM 能看到
- **不加特殊渲染**：build_user_content 正常渲染该 entry，因 msg_id 为空所以无 `[#]` 前缀，显示为 `[time] [系统](operator_qq): msg_id=xxx 的消息被撤回`

**冲突评估**：
- 伪消息 is_bot=False 但实际是系统消息，LLM 会把它当群成员消息看待。可接受——撤回通知本就是要让 LLM 看到的"事件"
- operator_qq 作为 qq 字段，nickname 用"系统"，LLM 能理解这是通知而非普通发言
- 伪消息不可引用（msg_id 为空），符合预期

---

## 二、需要优化的内容

> 在修改内容全部完成后，评估原有机制在新机制下是否可优化。

### 优化 1：快照机制删除（已在步骤 2 处理）

**现状**：`_rendered_pending_snapshot` 存 list[dict] 浅拷贝，原用途是 index→msg_id 映射。
**新机制下**：msg_id 由 LLM 直接输出，无需反查映射，快照无存在理由。
**决策**：**删除**（已在步骤 2 落实，此处仅记录决策依据）。
- 新方案下 msg_id 长期存在于 user_content 文本中，无需快照临时保存
- `get_msg_id_by_id` 不依赖快照，直接透传 msg_id 给 NapCat 校验
- 删除后 `build_user_content` 逻辑更简单，减少一次浅拷贝开销

### 优化 2：撤回通知的 persona 引导（必须做）

**现状**：persona 无任何关于撤回消息的引导。
**新机制下**：LLM 会看到 `[系统] (撤回通知): msg_id=xxx 的消息被撤回`，需引导 LLM 如何反应。
**优化内容**：在 persona.py 新增规则（规则 14 或并入场景响应章节）：
- 看到撤回通知时，根据上下文自然反应：
  - 没看到那句话/不感兴趣 → silent（真人不会对每条撤回都反应）
  - 没看到但好奇 → 可以问"撤了啥"
  - 看到了且感兴趣 → 可以吐槽"我都看到了"或调侃
  - 看到了但避而不谈 → silent（装作没看见）
- 反应符合当前上下文和人格，不要每次撤回都反应

### 优化 3：任务描述文档同步（必须做）

同步更新 `任务描述.md`：
- 历史记录格式章节：渲染格式从 `[N]` → `[#msg_id]`
- 输出协议章节：`target_msg_index` → `target_msg_id`，`react_target_msg_index` → `react_target_msg_id`
- 发送侧处理章节：reply 段解析逻辑更新
- 消息引用机制说明：更新快照用途
- 新增"撤回消息处理"章节
- persona 规则 8 同步更新
- 迭代顺序：新增阶段六（msg_id 重构 + 撤回支持）

---

## 三、需要删除的内容

> 在优化部分完成后，评估哪些机制已被取代、哪些内容已无用。

### 删除 1：快照机制 + get_msg_id_by_index 方法（已在步骤 2 处理）

**位置**：
- history.py L67 `_rendered_pending_snapshot` 字段
- history.py L189 快照赋值
- history.py L302-319 `get_msg_id_by_index` 方法
- history.py L64-66、L175-176 相关注释
**理由**：新方案下 msg_id 由 LLM 直接输出，无需 index→msg_id 反查映射，快照无存在理由；`get_msg_id_by_index` 被新方法 `get_msg_id_by_id` 取代。
**操作**：已在步骤 2 一并删除（此处仅记录，无单独删除步骤）。

### 删除 2：build_user_content 的编号逻辑

**位置**：history.py L191-192 `for i, m in enumerate(...)` 中的 `seq = i + 1`
**理由**：渲染格式不再使用正序编号。
**操作**：在步骤 1 修改时一并删除（不属于单独删除步骤，此处仅记录）。

### 删除 3：parser.py 的 index 校验逻辑

**位置**：parser.py L95-109（reply 段 target_msg_index int 校验）、L140-155（react_target_msg_index int 校验）
**理由**：被 msg_id str 校验取代。
**操作**：在步骤 3 修改时一并替换（此处仅记录）。

### 删除 4：persona.py 的 index 相关描述

**位置**：persona.py 规则 8 中 `[1][2][3]` 编号描述、`target_msg_index` 字段说明
**理由**：被 msg_id 描述取代。
**操作**：在步骤 6 修改时一并替换（此处仅记录）。

### 删除 5：任务描述.md 的 index 相关描述

**位置**：任务描述.md 多处 `target_msg_index`、`[N] 编号`描述
**理由**：文档同步。
**操作**：在优化 3 中一并处理（此处仅记录）。

---

## 执行顺序总览

1. **步骤 1-2**：history.py 渲染格式 + 新校验方法 + 删除快照机制
2. **步骤 3-5**：parser.py / message_sender.py / main.py 调用方同步
3. **步骤 6-7**：persona.py + llm_client.py 提示词更新
4. **步骤 8-10**：撤回消息功能（webhook 分发 + 处理逻辑 + 伪消息追加）
5. **优化 2-3**：persona 撤回引导 + 任务描述文档同步
6. **删除 2-5**：编号逻辑、index 校验、index 描述（在对应修改步骤中一并处理）

## 数据迁移策略

- 保留现有 `conversation.json`，不清空历史
- 老历史的 user_content 是 `[N]` 格式，无 msg_id 标记
- 老历史里的消息若被撤回，bot 可能无法定位（可接受，过渡期）
- 新消息自然带 `[#msg_id]` 格式
- 校验兜底：`get_msg_id_by_id` 对空串返回 None，避免崩溃

## 风险与兜底

- **LLM 抄错 msg_id**：`get_msg_id_by_id` 本轮校验 + sender 降级跳过 reply 段 + WARNING 日志
- **跨轮引用无本地校验**：直接透传 msg_id 给 NapCat，NapCat 会校验有效性
- **撤回通知伪消息被 LLM 误引用**：msg_id 为空，`get_msg_id_by_id` 返回 None，reply 段被跳过
- **并发竞态**：撤回通知追加 fast_buffer 持 `_buffer_lock`，与 drain 操作不冲突
