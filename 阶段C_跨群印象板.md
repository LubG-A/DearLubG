# 阶段 C：跨群印记板（Cross-Group Impression Board）

**目标**：让 bot 跨群感知"别的群最近在聊什么、有什么样的人"。每群独立维护 conversation，通过一个紧凑的、惰性更新的"印记板"注入 system prompt，让 LLM 像真人一样"对别的群有印象但不记得细节"。
**约束**：不破坏 per-group 历史独立性；印记是背景知识层，不替代历史；利用 system prompt 前缀缓存省 token。
**验证标准**：群 A 讨论某话题后，群 B 触发时 LLM 能在 thought 里体现"印象"，但 reply 不串台、不主动提起别群具体人事。

---

## 一、背景与动机

### 阶段 B 留下的"群间孤立"

阶段 B 实现了多群独立运行，每群有独立的 conversation/attribution/trigger，但**群与群之间完全孤立**：
- 群 A 的对话历史不会出现在群 B 的 LLM 上下文中。
- bot 在 A 群聊过的话题，到 B 群完全不知道。
- affinity 虽然跨群共享（按 QQ），但只是一个 0-100 的数字，没有"那个群在聊什么"的语义信息。

### 为什么不直接合并多群 conversation

最初讨论中已否决"合并多群 conversation"方案，核心问题：
1. **token 消耗大**：每次调用都送入大量跨群历史。
2. **上下文消费冲突**：群 A 的消息会消费群 B 的上下文窗口，导致 LLM 注意力分散，失去最近的群聊历史。
3. **语义不连贯**：两条不相干对话被强行拼在一起，是"长上下文注意力分散→幻觉串台"的变体。

### 印记板的核心思想

真人不会对所有群聊都记得非常细致，都是**有印象在聊什么、有什么样的人**。印记板模拟这种记忆模型：
- **每群一份独立 conversation**（阶段 B 已实现）——彻底消除跨群上下文消费问题。
- **跨群感知靠一个独立的、紧凑的"印记板"**——放在 system prompt 的固定位置，按事件惰性更新。
- **印记是"背景知识"不是"待回复内容"**——放 system prompt 语义正确，不干扰本轮 silent/reply 判断。

### 跨群信息转发的定位

跨群信息转发不依赖"LLM 主动请求检索别群 conversation"（该方案两轮 LLM 调用、状态管理、破坏 cycle 串行模型，复杂度过高）。**印记板本身就是跨群信息转发的机制**——A 群的信息通过印记摘要提取后注入 B 群的 system prompt。这是：
- **被动的**：惰性更新时提取，不是实时
- **语义层面的**：印象文本，不是精确到 message_id 的引用
- **不需要 LLM 主动请求**：信息已在 system prompt 里，LLM 直接可用

印记摘要 prompt 会引导 LLM 提取"话题 + 人物 + 关键事件"，使印记携带可引用的具体事件（如"王五说明天公司放假"），B 群讨论相关话题时 LLM 能自然关联。

### 阶段 C 的定位

阶段 C 引入 **CrossGroupImpressionStore**（跨群印记板）：
- 存储 per-group 的双维度印记：**群印记**（话题+关键事件）+ **人印记**（人物特征+关系）
- 注入 system prompt 的"# 其他群的近况"节（利用 DeepSeek 前缀缓存，token 成本几乎为零）
- 惰性更新：每群维护 trigger_count，达到阈值后追加一次廉价 LLM 摘要调用（纯活跃度驱动，无时间强制刷新）
- 印记附带时间戳，LLM 自行判断新鲜度；同时按时间戳排序选取最近活跃的 N 个群注入（硬性上限，防止 system prompt 累积）
- 身份关联用完整 QQ（唯一性），persona 规则约束不输出完整 QQ（隐私保护）

**阶段 C 不做**：
- 跨群历史直接引用（不把 A 群 history 搬到 B 群）
- LLM 主动请求检索别群 conversation（复杂度过高，收益不值得）
- per-group 归因跨群共享（不同群氛围不同，归因保持 per-group）
- 主动触发多群化（阶段 D）

---

## 二、设计决策与权衡

### 决策 C1：印记板维度——per-group，双维度（群印记 + 人印记）

**方案 A（per-group 群印记）**：`{group_id: {group_impression, ...}}`，每个群一条印记，描述"那个群最近在聊什么"。
**方案 B（per-user 人印象）**：`{qq: {impression, ...}}`，每个用户一条印象，描述"这个人怎么样"。
**方案 C（per-group 双维度）**：`{group_id: {group_impression, people_impression, ...}}`，每个群一条印记，同时包含群话题和人物特征。

**采用方案 C**。理由：
1. 群印记和人印记是互补的：群印记提供"话题背景"，人印记提供"人物特征"，与 affinity 数值协同（affinity 是亲密度数值，人印记是语义描述）。
2. 在一次 LLM 摘要调用中同时生成两者，成本不变但信息更丰富。
3. 人印记关注群内人物特征和**人物之间的关系**（如"张三和李四经常互怼"、"王五总是帮赵六说话"），让 LLM 更好地理解群内动态。
4. per-group 维度与 per-group conversation 对齐，摘要来源清晰（从该群 history 生成）。
5. per-group 印记天然隔离——A 群的印记只描述 A 群的近况，不会把 A 群某人的信息"泄漏"到 B 群的 per-user 上下文中。
6. token 开销可控：N 个群 = N 条印记，远小于 per-user（可能有几十个用户）。

### 决策 C2：注入位置——system prompt

**方案 A（system prompt）**：拼在"# 你的过往"之后，新增"# 其他群的近况"节。
**方案 B（user content）**：每次 cycle 渲染到 user content 中。

**采用方案 A**。理由：
1. **利用 DeepSeek 前缀缓存**：system prompt 走前缀缓存，印记更新频率低 → KV cache 命中率高 → token 成本几乎为零（仅注意力计算）。user content 每次都不同，无法命中缓存。
2. **语义正确**：印记是"背景知识"不是"待回复内容"，放 system prompt 不会干扰本轮 silent/reply 判断。如果放 user content 末尾，会被当作"本轮消息"参与 LLM 的"是否回复"判断。
3. **位置固定**：system prompt 的结构稳定，印记节始终在"# 你的过往"之后、"# 早期对话摘要"之前，LLM 学习成本低。

**渲染格式**（拼在"# 你的过往"之后，含时间戳）：
```
# 其他群的近况（印象，非本次对话内容）
- 群 945024095（2小时前）：
  话题：最近在讨论 React 19 的新特性，张三(1234567890)在吹牛说自己写的 hooks 比官方还好。
  人物：李四(1234567891)比较安静偶尔吐槽；王五(1234567892)和赵六(1234567893)经常互怼。
- 群 123456789（3天前）：
  话题：昨晚几个人约了原神深渊，李四(1234567891)放鸽子被吐槽。
  人物：王五(1234567892)说明天公司放假。
注：这些只是你对别的群的模糊印象，时间越久越可能过时，不要当作确切事实引用。不要在当前群里主动提起别群的具体人或事，除非自然关联。
```

空时填"（无）"：
```
# 其他群的近况（印象，非本次对话内容）
（无）
```

### 决策 C3：更新机制——纯活跃度驱动的惰性更新

**方案 A（纯 trigger_count）**：每群维护 trigger_count，达到阈值后追加 LLM 摘要调用。
**方案 B（trigger_count + 时间强制刷新）**：额外加"超过 N 小时强制更新"。
**方案 C（LLM 在线输出）**：每次 cycle LLM 顺便输出印记更新。

**采用方案 A**。理由：
1. **更新与否依靠群内消息活跃度而不是时间**：冷群 trigger_count 增长慢，印记更新慢，自然保持旧状态；热群频繁触发，印记保持新鲜。符合真人"好久没看那个群了"的记忆衰减。
2. **时间强制刷新无意义**：如果群不活跃，强制刷新也只是用旧数据重新摘要，浪费 LLM 调用。印记是否过时应由 LLM 通过时间戳自行判断，而非硬性规则。
3. **不增加主 cycle 的 LLM 负担**：方案 C 要求 LLM 每次都输出印记更新，增加输出 token 和提示词复杂度。方案 A 的摘要调用是独立的、廉价的。

**惰性更新逻辑**：
```
每次 LLM cycle 成功完成（实际调用了 LLM，非 pending 空、非延迟回复）：
  1. trigger_count += 1
  2. 如果 trigger_count >= 5：
     a. 取该群近 N 轮 user content（从 history.messages 取最后 N 条 role=user 的 content）
     b. 调 LLM 摘要生成群印记 + 人印记（专用 prompt，输出 JSON，用完整 QQ 标注、关注人物关系）
     c. 更新 impressions[group_id] = {group_impression, people_impression, updated_at=now, trigger_count=0}
     d. 持久化到 state/impressions.json
  3. 否则：只更新 trigger_count，不调 LLM
```

**关键参数**：
- `TRIGGER_THRESHOLD = 5`：触发 5 次后更新印记
- `IMPRESSION_SNAPSHOT_TURNS = 10`：取近 10 轮 user content 做摘要输入
- `IMPRESSION_MAX_LEN = 200`：每条印记（群+人）总文本最大 200 字

### 决策 C4：新鲜度判断与注入上限——时间戳提示 + 排序选取

**方案 A（硬性 TTL 淡出）**：超过 N 天未更新的群不进入印记板。
**方案 B（纯时间戳提示，无限制）**：所有有印记的群都注入，LLM 自行判断新鲜度。
**方案 C（时间戳排序选取）**：按 updated_at 降序，只注入最近活跃的 N 个群的印记，附带时间戳。

**采用方案 C**。理由：
1. **保证 system prompt 大小可控**：硬性上限 N 个群 × 200 字 = 固定上限，防止群数量增长时 system prompt 无限膨胀。
2. **比 TTL 更平滑**：TTL 边界处印记突然消失，行为不连贯；排序选取是最旧的被挤掉，重新活跃后会重新出现。
3. **与时间戳提示协同**：被选中的印记仍带时间戳（"2小时前"/"3天前"），LLM 仍能判断过时程度。
4. **符合真人记忆**：人只能记住有限数量的群的近况，最久没看的群印象最模糊。

**参数**：`MAX_IMPRESSION_GROUPS = 5`（只注入最近活跃的 5 个群的印记）

**实现**：`get_others_impressions` 按 `updated_at` 降序排序，排除当前群后取前 5 个。

**persona 规则引导**："时间越久越可能过时，不要当作确切事实引用。"

**时间戳格式**：相对时间（如"2小时前"、"3天前"），由 `get_others_impressions` 在渲染时计算。

### 决策 C5：身份关联与隐私保护——完整 QQ 标注 + persona 规则

**问题**：群 A 印记里提到"张三说想离职"，群 B 也有张三，LLM 可能在群 B 里幻觉出"你不是说要离职吗"。

**采用**：
1. **完整 QQ 标注**：印记摘要中提到的人用"昵称(完整QQ)"格式，如"张三(1234567890)"。完整 QQ 唯一，确保跨群身份关联准确，避免尾号标注的歧义。
2. **persona 规则引导**：system prompt 新增规则——
   - "印记里的人用完整 QQ 标注，同 QQ 在不同群是同一个人，但他在不同群的表现可能不同。"
   - "不要在当前群里主动提起别群的具体人或事，除非话题自然关联。"
   - **"出于隐私保护，不要在回复中直接输出别人的完整 QQ 号。"**

理由：
1. 完整 QQ 唯一性确保跨群关联准确，避免尾号标注的歧义（不同人可能尾号相同）。
2. 隐私保护：完整 QQ 只在 system prompt 内部用于关联，persona 规则约束 LLM 不在回复中输出完整 QQ，避免泄露。
3. 把"跨群身份关联"的决定权交给 LLM 的语义判断——同 QQ 是同一人，但不同群表现可能不同，LLM 根据上下文决定是否关联引用。

### 决策 C6：存储位置——全局文件 `state/impressions.json`

**方案 A（全局文件）**：`state/impressions.json` 存储所有群的印记，结构为 `{group_id: {group_impression, people_impression, updated_at, trigger_count}}`。
**方案 B（per-group 文件）**：`state/{group_id}/impressions.json` 存储该群自己的印记。

**采用方案 A**。理由：
1. **数据加载简单**：一次读取整个文件到内存，无需遍历目录。
2. **代码编写便利**：`_load`/`_save` 一次操作整个文件，无需按群分文件读写。
3. **删除同样便利**：`{group_id: {...}}` 结构中，删除某群的印记只需删对应的 key 再保存，与删目录同样简单。
4. **与 affinity.json 一致**：两者都是全局跨群数据，用全局文件合理。

目标结构：
```
state/
├── {group_id}/                    # per-group（阶段 B）
│   ├── conversation.json
│   ├── state.json
│   └── attribution_log.jsonl
├── affinity.json                  # 全局（跨群亲密度数值）
├── impressions.json               # 全局（跨群印记文本）← 阶段 C 新增
├── background_story.md            # 全局（角色背景）
├── background_summary.json        # 全局
└── scheduler.json                 # 全局（阶段 D 移入子目录）
```

**全局文件内容**（`state/impressions.json`）：
```json
{
  "945024095": {
    "group_impression": "最近在讨论 React 19 的新特性",
    "people_impression": "张三(1234567890)喜欢吹牛；李四(1234567891)和赵六(1234567892)经常互怼",
    "updated_at": "2026-07-01T21:48:27",
    "trigger_count": 3
  },
  "123456789": {
    "group_impression": "昨晚几个人约了原神深渊",
    "people_impression": "李四(1234567891)放鸽子被吐槽",
    "updated_at": "2026-07-01T20:00:00",
    "trigger_count": 0
  }
}
```

### 决策 C7：trigger_count 归属——放在 impressions.json 中

**方案 A（放 impressions.json）**：trigger_count 与印记数据一起持久化。
**方案 B（放 GroupContext）**：GroupContext 新增 `trigger_count_since_impression_update: int` 字段。

**采用方案 A**。理由：
1. trigger_count 是印记板的内部状态，与印记文本一起持久化更内聚。
2. 放 GroupContext 需要额外的持久化逻辑（GroupContext 目前不持久化运行时字段，只通过 history/attribution 间接持久化）。
3. 重启后 trigger_count 恢复——如果放内存，重启后归零，可能导致频繁触发摘要调用。

### 决策 C8：摘要 LLM 调用——新增 summarize_for_impression 方法

**方案 A（复用 llm.summarize）**：直接用现有的历史摘要方法。
**方案 B（新增 summarize_for_impression）**：专用 prompt，生成群印记+人印记 JSON。

**采用方案 B**。理由：
1. 现有 `summarize` 的 prompt 要求"压缩成 400 字要点，保留 msg_id 标记"——这是历史压缩用的，不适合印记板。
2. 印记板需要的是"群话题+人物特征"双维度 JSON，要求完整 QQ 标注、关注人物关系、≤200 字——prompt 完全不同。
3. 专用方法可以独立调优 prompt，不影响历史压缩的质量。

**新增方法签名**：
```python
def summarize_for_impression(self, group_id: str, recent_user_contents: list[str]) -> Optional[dict]:
    """为印记板生成群印记+人印记。

    Args:
        group_id: 群号（用于日志）
        recent_user_contents: 近 N 轮 user content 文本列表

    Returns:
        {"topic": "群话题文本", "people": "人物特征文本"}，失败返回 None
    """
```

**专用 prompt**：
```
你是群聊印象助手。根据以下群聊消息，生成这个群的印象，包含两部分：
1. topic：1句话描述"这个群最近在聊什么"，包括主要话题和关键事件（如日程、约定、重要决定）
2. people：1-2句话描述"群里的人有什么特征和关系"，用"昵称(完整QQ)"格式标注人物

要求：
- 提到的人一律用"昵称(完整QQ)"格式，如"张三(1234567890)"，确保跨群身份关联准确
- people 部分关注人物性格特征和人物之间的关系（如谁和谁经常互怼、谁总是帮谁说话）
- topic 和 people 各 ≤100 字
- 只输出 JSON，不要任何前缀或解释，格式：{"topic": "...", "people": "..."}
```

**与 `summarize` 的区别**：
- prompt 不同：`summarize` 压缩成 400 字要点（保留 msg_id）；`summarize_for_impression` 生成双维度 JSON（完整 QQ 标注、人物关系）。
- timeout 不同：`summarize` 用 120s（历史压缩量大）；`summarize_for_impression` 用 60s（输入小）。
- 输入不同：`summarize` 接收 `list[dict]`（user/assistant 对话）；`summarize_for_impression` 接收 `list[str]`（仅 user content）。
- 输出不同：`summarize` 返回 `str`；`summarize_for_impression` 返回 `dict`（JSON 解析后）。

**容错**：实际测试中 1378 次 API 调用仅 1 次 JSON 格式解析错误，`json.loads` 失败则记 WARNING 返回 None，保留旧印记。不做更复杂的正则提取。

### 决策 C9：空状态处理——初始为空

**采用**：启动时无印记文件则初始化为空。不主动为现有群生成印记。

理由：
1. 没有历史数据可供生成印记——印记需要"近 N 轮 user content"，但已有的 history 是完整的 conversation（不是近 N 轮快照）。
2. 空印记不影响功能——system prompt 的"# 其他群的近况"节填"（无）"，LLM 行为与阶段 B 一致。
3. 印记会随着后续互动自然积累，不需要初始化。
4. 避免启动时额外的 LLM 调用。

### 决策 C10：CrossGroupImpressionStore 归属——全局单例 + 全局文件

**方案 A（全局单例 + 全局文件）**：CrossGroupImpressionStore 是全局单例，`state/impressions.json` 存所有群印记。
**方案 B（全局单例 + per-group 文件）**：CrossGroupImpressionStore 是全局单例，但文件按群隔离 `state/{group_id}/impressions.json`。
**方案 C（per-group 单例）**：每个 GroupContext 持有自己的印记，需跨群读取。

**采用方案 A**。理由：
1. **全局单例的便利性**：`_run_llm_cycle` 在 main.py 的 Bot 类中，直接用 `self.impression_board` 即可，避免给 GroupContext 增加不必要的引用。
2. **全局文件加载简单**：一次读取 `state/impressions.json` 到内存，无需遍历目录。
3. **删除同样便利**：`{group_id: {...}}` 结构中，删除某群的印记只需删对应的 key 再保存，与删目录同样简单。
4. **与 AffinityManager 一致**：两者都是全局跨群数据，用全局单例 + 全局文件合理。

---

## 三、具体改动清单

### 3.1 新建 `src/impression.py`

```python
"""跨群印记板（Cross-Group Impression Board）。

存储 per-group 的双维度印记（群话题 + 人物特征），跨群共享。
注入 system prompt 的"# 其他群的近况"节，让 LLM 像真人一样"对别的群有印象但不记得细节"。

存储结构（全局文件）：
  state/impressions.json
  内容: {group_id: {"group_impression": "...", "people_impression": "...", "updated_at": "...", "trigger_count": N}}

更新机制：惰性更新——每群维护 trigger_count，达到阈值（5次）后追加 LLM 摘要调用。纯活跃度驱动，无时间强制刷新。
新鲜度判断：印记附带相对时间戳注入 system prompt，LLM 自行判断过时程度。
注入上限：按 updated_at 降序，只注入最近活跃的 MAX_IMPRESSION_GROUPS 个群的印记（硬性上限）。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from .utils.logger import get_logger

logger = get_logger("impression")

# 惰性更新参数
TRIGGER_THRESHOLD = 5              # 触发 5 次后更新印记
IMPRESSION_SNAPSHOT_TURNS = 10     # 取近 10 轮 user content 做摘要输入
IMPRESSION_MAX_LEN = 200           # 每条印记（群+人）总文本最大 200 字

# 注入上限
MAX_IMPRESSION_GROUPS = 5          # 只注入最近活跃的 5 个群的印记


class CrossGroupImpressionStore:
    """跨群印记板管理器（全局单例，全局文件存储）。"""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = Path(state_dir)
        self.file = self.state_dir / "impressions.json"
        # 内存缓存：{group_id: {group_impression, people_impression, updated_at, trigger_count}}
        self.impressions: dict[str, dict] = {}
        self._load()

    def _load(self):
        """加载印记数据。文件不存在则初始化为空。"""
        if not self.file.exists():
            self.impressions = {}
            return
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                self.impressions = json.load(f)
            logger.info(f"加载印记数据：{len(self.impressions)} 个群有印记")
        except Exception as e:
            logger.error(f"加载印记数据失败: {e}，初始化为空")
            self.impressions = {}

    def _save(self):
        """原子写入：临时文件 + rename。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.impressions, f, ensure_ascii=False, indent=2)
        tmp.replace(self.file)

    @staticmethod
    def _format_relative_time(updated_at_str: str) -> str:
        """将 ISO 时间戳格式化为相对时间（如'2小时前'、'3天前'）。"""
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            delta = datetime.now() - updated_at
            seconds = int(delta.total_seconds())
            if seconds < 60:
                return "刚刚"
            if seconds < 3600:
                return f"{seconds // 60}分钟前"
            if seconds < 86400:
                return f"{seconds // 3600}小时前"
            return f"{seconds // 86400}天前"
        except (ValueError, TypeError):
            return "未知时间"

    def get_others_impressions(self, exclude_group_id: str) -> str:
        """获取其他群的印记（排除当前群），拼进 system prompt。

        按 updated_at 降序排序，只取最近活跃的 MAX_IMPRESSION_GROUPS 个群。
        附带相对时间戳让 LLM 判断新鲜度。
        空时返回 "（无）"。

        Args:
            exclude_group_id: 当前群 ID（不展示自己的印记）

        Returns:
            拼接好的印记文本，如：
            "- 群 945024095（2小时前）：
              话题：最近在讨论 React 19...
              人物：张三(1234567890)喜欢吹牛..."
        """
        # 收集候选印记（排除当前群、无 updated_at 的跳过）
        candidates = []
        for gid, entry in self.impressions.items():
            if gid == exclude_group_id:
                continue
            updated_at_str = entry.get("updated_at", "")
            if not updated_at_str:
                continue
            group_text = entry.get("group_impression", "")
            people_text = entry.get("people_impression", "")
            if not group_text and not people_text:
                continue
            try:
                updated_at = datetime.fromisoformat(updated_at_str)
            except ValueError:
                continue
            candidates.append((updated_at, gid, group_text, people_text, updated_at_str))

        # 按 updated_at 降序，取前 MAX_IMPRESSION_GROUPS 个
        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[:MAX_IMPRESSION_GROUPS]

        if not candidates:
            return "（无）"

        lines = []
        for _, gid, group_text, people_text, updated_at_str in candidates:
            rel_time = self._format_relative_time(updated_at_str)
            lines.append(f"- 群 {gid}（{rel_time}）：")
            if group_text:
                lines.append(f"  话题：{group_text}")
            if people_text:
                lines.append(f"  人物：{people_text}")

        return "\n".join(lines)

    def maybe_update(self, group_id: str, recent_user_contents: list[str],
                     summarize_fn: Callable[[str, list[str]], Optional[dict]]):
        """惰性更新：检查触发次数，达到阈值则调 LLM 摘要。

        在每次 LLM cycle 成功完成后调用（_handle_result 之后）。
        纯活跃度驱动（trigger_count >= 阈值），无时间强制刷新。

        Args:
            group_id: 当前群 ID
            recent_user_contents: 近 N 轮 user content 文本列表（从 history.messages 取）
            summarize_fn: LLM 摘要函数，签名 (group_id, recent_user_contents) -> Optional[dict]
                          dict 格式: {"topic": "...", "people": "..."}
        """
        entry = self.impressions.get(group_id, {
            "group_impression": "",
            "people_impression": "",
            "updated_at": "",
            "trigger_count": 0,
        })
        entry["trigger_count"] = entry.get("trigger_count", 0) + 1

        if entry["trigger_count"] >= TRIGGER_THRESHOLD and recent_user_contents:
            logger.info(f"更新群 {group_id} 印记：触发次数达阈值({entry['trigger_count']}>={TRIGGER_THRESHOLD})")
            result = summarize_fn(group_id, recent_user_contents)
            if result:
                topic = (result.get("topic") or "")[:IMPRESSION_MAX_LEN // 2]
                people = (result.get("people") or "")[:IMPRESSION_MAX_LEN // 2]
                entry["group_impression"] = topic
                entry["people_impression"] = people
                entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
                entry["trigger_count"] = 0
                logger.info(f"群 {group_id} 印记更新成功：话题={topic[:30]}... 人物={people[:30]}...")
            else:
                logger.warning(f"群 {group_id} 印记摘要失败，保留旧印记")
                # 摘要失败不重置 trigger_count，下次再试

        self.impressions[group_id] = entry
        self._save()
```

### 3.2 修改 `src/llm_client.py`

#### 3.2.1 新增 `summarize_for_impression` 方法

```python
_IMPRESSION_SUMMARY_PROMPT = (
    "你是群聊印象助手。根据以下群聊消息，生成这个群的印象，包含两部分：\n"
    "1. topic：1句话描述\"这个群最近在聊什么\"，包括主要话题和关键事件（如日程、约定、重要决定）\n"
    "2. people：1-2句话描述\"群里的人有什么特征和关系\"，用\"昵称(完整QQ)\"格式标注人物\n\n"
    "要求：\n"
    "- 提到的人一律用\"昵称(完整QQ)\"格式，如\"张三(1234567890)\"，确保跨群身份关联准确\n"
    "- people 部分关注人物性格特征和人物之间的关系（如谁和谁经常互怼、谁总是帮谁说话）\n"
    "- topic 和 people 各 ≤100 字\n"
    "- 只输出 JSON，不要任何前缀或解释，格式：{\"topic\": \"...\", \"people\": \"...\"}"
)

def summarize_for_impression(self, group_id: str, recent_user_contents: list[str]) -> Optional[dict]:
    """为印记板生成群印记+人印记。

    Args:
        group_id: 群号（用于日志）
        recent_user_contents: 近 N 轮 user content 文本列表

    Returns:
        {"topic": "群话题文本", "people": "人物特征文本"}，失败返回 None
    """
    if not recent_user_contents:
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {self.api_key}",
    }

    # 拼接近 N 轮 user content（只取消息正文部分，每轮后 300 字）
    lines = []
    for content in recent_user_contents:
        text = content[-300:] if len(content) > 300 else content
        lines.append(text)
    dialog_text = "\n---\n".join(lines)

    messages = [
        {"role": "system", "content": self._IMPRESSION_SUMMARY_PROMPT},
        {"role": "user", "content": f"以下是群 {group_id} 近期的群聊消息：\n\n{dialog_text}"},
    ]

    payload = {
        "model": self.model,
        "messages": messages,
    }

    try:
        resp = requests.post(self.api_url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        # 解析 JSON 输出
        result = json.loads(content)
        if not isinstance(result, dict) or "topic" not in result or "people" not in result:
            logger.warning(f"群 {group_id} 印记摘要 JSON 格式异常: {content[:100]}")
            return None
        logger.info(f"群 {group_id} 印记摘要生成成功: topic={result['topic'][:30]}... people={result['people'][:30]}...")
        return result
    except json.JSONDecodeError as e:
        logger.warning(f"群 {group_id} 印记摘要 JSON 解析失败: {e}")
        return None
    except requests.RequestException as e:
        logger.warning(f"群 {group_id} 印记摘要请求失败: {e}")
        return None
    except (KeyError, IndexError) as e:
        logger.warning(f"群 {group_id} 印记摘要响应解析失败: {e}")
        return None
```

**与 `summarize` 的区别**：
- prompt 不同：`summarize` 压缩成 400 字要点（保留 msg_id）；`summarize_for_impression` 生成双维度 JSON（完整 QQ 标注、人物关系）。
- timeout 不同：`summarize` 用 120s（历史压缩量大）；`summarize_for_impression` 用 60s（输入小）。
- 输入不同：`summarize` 接收 `list[dict]`（user/assistant 对话）；`summarize_for_impression` 接收 `list[str]`（仅 user content）。
- 输出不同：`summarize` 返回 `str`；`summarize_for_impression` 返回 `dict`（JSON 解析后）。

**容错**：实际测试中 1378 次 API 调用仅 1 次 JSON 格式解析错误，`json.loads` 失败则记 WARNING 返回 None，保留旧印记。不做更复杂的正则提取。

### 3.3 修改 `src/persona.py`

#### 3.3.1 `render_system_prompt` 新增 `others_impressions` 参数

```python
def render_system_prompt(self, summary: str = "", others_impressions: str = "") -> str:
    """渲染系统提示词。

    Args:
        summary: 早期对话摘要（拼到末尾）
        others_impressions: 其他群的印记文本（拼在"# 你的过往"之后）
    """
    p = self.config.persona
    prompt = self.SYSTEM_PROMPT_TEMPLATE.format(
        name=p.name,
        # ... 现有参数不变 ...
        background=self._background if self._background else "（无）",
    )

    # 新增：# 其他群的近况 节（拼在"# 你的过往"之后、"# 早期对话摘要"之前）
    if others_impressions:
        prompt += (
            f"\n\n# 其他群的近况（印象，非本次对话内容）\n{others_impressions}\n"
            f"注：这些只是你对别的群的模糊印象，时间越久越可能过时，不要当作确切事实引用。"
            f"不要在当前群里主动提起别群的具体人或事，除非自然关联。"
        )

    if summary:
        prompt += f"\n\n# 早期对话摘要（你之前看过的消息和回复的要点）\n{summary}"
    return prompt
```

#### 3.3.2 system prompt 新增规则：跨群印记使用指导

在 `SYSTEM_PROMPT_TEMPLATE` 的硬约束节中新增规则：

```
13. "# 其他群的近况"节列的是你对别的群的模糊印象，是背景知识不是待回复内容。
    - 印记里的人用完整 QQ 标注，同 QQ 在不同群是同一个人，但他在不同群的表现可能不同。
    - 不要在当前群里主动提起别群的具体人或事，除非话题自然关联。
    - 印记附带时间戳，时间越久越可能过时，不要当作确切事实引用。
    - 出于隐私保护，不要在回复中直接输出别人的完整 QQ 号。
```

### 3.4 修改 `main.py`

#### 3.4.1 `__init__` 新增 CrossGroupImpressionStore

```python
from src.impression import CrossGroupImpressionStore

# 全局共享单例（与 affinity 同级，跨群共享）
self.affinity = AffinityManager()
self.impression_board = CrossGroupImpressionStore()  # 新增
```

#### 3.4.2 `_run_llm_cycle` 改动

**步骤 4**：构建 system_prompt 时注入其他群印记：

```python
# 4. 构建 user_content
summary = ctx.history.get_summary()
# 新增：获取其他群印记
others_impressions = self.impression_board.get_others_impressions(ctx.group_id)
system_prompt = self.persona_renderer.render_system_prompt(
    summary, others_impressions=others_impressions
)
```

**步骤 7 之后**：惰性更新印记（只在 LLM 实际调用成功后，非延迟回复）：

```python
# 7. 发送 + 归因
self._handle_result(ctx, parsed, soft_factors)

# 7.5 印记板惰性更新（LLM 调用成功后，取近 N 轮 user content 做摘要）
recent_user_contents = ctx.history.get_recent_user_contents(IMPRESSION_SNAPSHOT_TURNS)
self.impression_board.maybe_update(
    ctx.group_id, recent_user_contents, self.llm.summarize_for_impression
)

# 8. 检查重试...
```

**注意**：延迟回复的情况（`reply_delay_minutes > 0 and action == "silent"`，步骤 6 的 return）不调 `maybe_update`——因为没有产生实际对话，消息存到了 delayed_replies。

#### 3.4.3 `warmup` 改动

CrossGroupImpressionStore 在 `__init__` 时已加载数据，`warmup` 无需额外操作。可加一行日志：

```python
logger.info(f"跨群印记板：{len(self.impression_board.impressions)} 个群有印记")
```

### 3.5 修改 `src/history.py`

#### 3.5.1 新增 `get_recent_user_contents` 方法

```python
def get_recent_user_contents(self, n: int = 10) -> list[str]:
    """获取近 N 轮 user content（用于印记板摘要输入）。

    Args:
        n: 取最近 N 条 role=user 的 content

    Returns:
        user content 文本列表，按时间正序（旧→新）
    """
    user_msgs = [m for m in self.messages if m.get("role") == "user"]
    return [m["content"] for m in user_msgs[-n:] if "content" in m]
```

### 3.6 不需要修改的文件

| 文件 | 原因 |
|---|---|
| `src/affinity.py` | 独立存储，不合并印记 |
| `src/attribution.py` | 归因保持 per-group，不跨群 |
| `src/trigger.py` | 触发评分不读印记 |
| `src/group_context.py` | 印记是全局单例，不注入 GroupContext |
| `src/parser.py` | 不扩展 LLM 输出协议（印记由独立 LLM 调用生成，非主 cycle 输出） |
| `src/napcat_client.py` | 无关 |
| `src/senders/*` | 无关 |
| `config.yaml` | 无新增配置项（阈值是代码常量） |

---

## 四、验证方法

### 4.1 单群兼容验证

config.yaml 仍为单群（`group_ids: ["341353242"]`），启动后：
1. 无印记文件 → 初始化为空，日志输出"跨群印记板：0 个群有印记"。
2. 被动触发正常，system prompt 的"# 其他群的近况"节填"（无）"（只有自己一个群，无其他群印记）。
3. LLM cycle 成功后，`maybe_update` 递增 trigger_count。
4. 触发 5 次后，印记更新，`state/impressions.json` 中出现该群的印记条目（含 group_impression/people_impression/updated_at/trigger_count=0）。

### 4.2 跨群印记验证

config.yaml 配置两个群 `["A", "B"]`，验证流程：
1. 群 A 触发 5 次 LLM cycle，`maybe_update` 调 `summarize_for_impression` 生成印记。
2. 检查 `state/impressions.json` 中 A 的印记已写入（含 group_impression/people_impression/updated_at/trigger_count=0）。
3. 群 B 触发 LLM cycle，检查 system prompt 中出现"# 其他群的近况"节，包含 A 的印记（话题+人物+相对时间戳）。
4. LLM 的 thought 中应体现对 A 群的印象（如"那边好像在聊原神"），但 reply 不串台（不在 B 群主动提起 A 群具体人事）。

### 4.3 排序选取上限验证

1. 配置 7 个群（超过 MAX_IMPRESSION_GROUPS=5），每个群都触发 5 次生成印记。
2. 任一群触发 cycle，检查 system prompt 的"# 其他群的近况"节——只包含最近活跃的 5 个群的印记（排除自己），第 6 个群的印记不出现。
3. 修改某群的 `updated_at` 为最新，该群应出现在印记节中，最旧的群被挤掉。

### 4.4 时间戳新鲜度验证

1. 群 A 印记更新后，手动修改 `state/impressions.json` 中 A 的 `updated_at` 为 3 天前。
2. 群 B 触发 cycle，检查 system prompt 中 A 的印记附带"3天前"时间戳。
3. LLM 应理解该印象可能过时，在 thought 中谨慎引用（如"那边好像之前在聊X，不过有几天了"）。

### 4.5 身份关联与隐私保护验证

1. 群 A 印记中提到"张三(1234567890)说想离职"。
2. 群 B 也有张三(1234567890)，触发 cycle。
3. LLM 不应在 B 群对张三说"你不是说要离职吗"——除非张三主动表明身份。
4. 检查 LLM 的 reply 中不包含完整 QQ 号"1234567890"（隐私保护规则）。
5. 检查印记文本中的人名格式是否为"昵称(完整QQ)"。

### 4.6 人物关系验证

1. 群 A 的对话中张三和李四经常互怼。
2. 群 A 触发 5 次后，检查印记的 people_impression 是否包含关系描述（如"张三(1234567890)和李四(1234567891)经常互怼"）。
3. 群 B 的 LLM 应能在 thought 中理解这种关系（如果 B 群也有这两人）。

### 4.7 边界情况验证

1. **LLM 摘要失败**：`summarize_for_impression` 返回 None → 保留旧印记，不重置 trigger_count，下次再试。
2. **LLM 摘要 JSON 格式异常**：`json.loads` 失败 → 记 WARNING，返回 None，保留旧印记。
3. **impressions.json 损坏**：加载失败，记 ERROR，初始化为空，不阻塞启动。
4. **印记文本超长**：`maybe_update` 中 topic/people 各截断到 100 字。
5. **延迟回复的 cycle**：不调 `maybe_update`（消息存到 delayed_replies，无实际对话）。
6. **pending 空的 cycle**：不调 `maybe_update`（没调 LLM，无新内容）。
7. **退群清理**：删除 `state/impressions.json` 中该群的 key，其他群的印记不受影响。

### 4.8 日志关键词检查

- `跨群印记板：N 个群有印记`（启动时）
- `加载印记数据：N 个群有印记`（初始化时）
- `更新群 xxx 印记：触发次数达阈值(5>=5)`
- `群 xxx 印记更新成功：话题=... 人物=...`
- `群 xxx 印记摘要失败，保留旧印记`
- `群 xxx 印记摘要生成成功: topic=... people=...`
- `群 xxx 印记摘要 JSON 格式异常` / `群 xxx 印记摘要 JSON 解析失败`

不应出现：
- `state/impressions.json` 写入失败导致 traceback
- LLM 在当前群主动提起别群具体人事（除非自然关联）
- LLM 在回复中输出完整 QQ 号
- 印记注入导致 system prompt 过长（应受 MAX_IMPRESSION_GROUPS 控制）

---

## 五、不在本次范围内的事项

| 事项 | 阶段 |
|---|---|
| LLM 主动请求检索别群 conversation（精确到 message_id 的转发） | 不做（复杂度过高，收益不值得） |
| 独立的关键信息层（结构化提取日程/约定） | 未来（如果印记的语义层转发不够用） |
| TriggerEvaluator 读取印记影响触发评分 | 未来（当前印记只影响 LLM 上下文） |
| 主动触发多群化 | 阶段 D |
| 群名称展示（当前只显示群号） | 未来（需 NapCat 获取群名称） |
| 印记的 UI 展示/管理工具 | 不做 |

---

## 六、风险与回滚

### 风险

1. **额外的 LLM 摘要调用增加成本**：每次印记更新调一次 `summarize_for_impression`。
   - 分析：触发频率受阈值（5 次 cycle）控制，纯活跃度驱动。冷群不更新，热群每 5 次 cycle 更新一次。
   - 输入小（近 10 轮 user content 的后 300 字 ≈ 3KB），输出短（双维度 JSON ≤200 字），单次成本低。
   - 缓解：如果成本仍高，可调大 `TRIGGER_THRESHOLD`。

2. **印记摘要质量差**：LLM 生成的印记可能不够准确或过于笼统。
   - 缓解：专用 prompt 明确要求"话题+关键事件，人物特征+关系，完整 QQ 标注，≤200 字"。
   - 监控：可定期检查 `state/impressions.json` 内容质量。

3. **system prompt 变长影响前缀缓存**：印记节增加 system prompt 长度。
   - 分析：印记节最多 MAX_IMPRESSION_GROUPS 个群 × ~200 字 ≈ 1KB，有硬性上限。
   - 缓解：印记更新频率低（每 5 次 cycle），system prompt 变化频率低，前缀缓存命中率仍高。

4. **LLM 生硬引用印记**：LLM 可能直接说"群 A 在聊原神"，不够自然。
   - 缓解：persona 规则 13 明确要求"不要在当前群里主动提起别群的具体人或事，除非自然关联"。
   - 这是 LLM 提示工程问题，需迭代调优。

5. **完整 QQ 隐私泄露**：LLM 可能在回复中输出完整 QQ 号。
   - 缓解：persona 规则 13 明确约束"出于隐私保护，不要在回复中直接输出别人的完整 QQ 号"。
   - 监控：可定期检查 LLM 回复是否包含完整 QQ 号。

### 回滚

阶段 C 回滚步骤：
1. `git revert` 代码改动。
2. 删除 `state/impressions.json`（可选，留着也不影响）。
3. Bot 行为回到阶段 B（无跨群印记，system prompt 无"# 其他群的近况"节）。

回滚成本低：CrossGroupImpressionStore 是独立模块，不与现有系统耦合。删除后不影响 affinity/history/attribution/trigger 任何现有功能。
