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
