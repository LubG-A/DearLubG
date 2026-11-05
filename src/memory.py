"""记忆管理（per-chat）：长期记忆 core / 近期日记 journal / 短期备忘 memo。

设计要点（与阶段 C 的缓存教训保持一致）：
- core 放 system prompt，只在记忆整理时变化（低频，断缓存代价可接受）；
- journal / memo 放 user content，每轮变化也不影响历史消息的前缀缓存。

存储布局（每个 chat 一个目录）：
    state/{group_id}/memory/core.md
    state/{group_id}/memory/journal.jsonl
    state/{group_id}/memory/memo.json
    state/private/{qq}/memory/...

并发：写操作只在 LLM 工作线程（普通 cycle / 记忆整理 cycle）内发生，
全局 cycle 串行保证不会并发写同一个文件。
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import MemoryConfig
from .utils.logger import get_logger

logger = get_logger("memory")


class MemoryManager:
    """单个聊天（群聊 / 私聊）的记忆管理器。"""

    def __init__(self, config, state_dir: str):
        self.cfg: MemoryConfig = getattr(config, "memory", None) or MemoryConfig()
        self.enabled: bool = bool(self.cfg.enabled)
        self.memo_enabled: bool = self.enabled and bool(self.cfg.memo_enabled)

        self.state_dir = Path(state_dir)
        self.memory_dir = self.state_dir / "memory"
        self.core_file = self.memory_dir / "core.md"
        self.journal_file = self.memory_dir / "journal.jsonl"
        self.memo_file = self.memory_dir / "memo.json"

        self._core: str = ""
        self._journal: list[dict] = []
        self._memos: list[dict] = []
        self._next_journal_id: int = 1
        self._next_memo_id: int = 1

        if self.enabled:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.core_file.exists():
            try:
                self._core = self.core_file.read_text(encoding="utf-8").strip()
            except OSError as e:
                logger.warning(f"加载长期记忆失败: {e}")

        if self.journal_file.exists():
            try:
                for line in self.journal_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict) or not entry.get("text"):
                        continue
                    try:
                        entry_id = int(entry.get("id", self._next_journal_id))
                    except (TypeError, ValueError):
                        entry_id = self._next_journal_id
                    entry["id"] = entry_id
                    entry.setdefault("created_at", 0.0)
                    entry.setdefault("time", "")
                    self._journal.append(entry)
                    self._next_journal_id = max(self._next_journal_id, entry_id + 1)
            except OSError as e:
                logger.warning(f"加载日记失败: {e}")

        if self.memo_file.exists():
            try:
                data = json.loads(self.memo_file.read_text(encoding="utf-8"))
                for item in data.get("items", []):
                    if not isinstance(item, dict) or not item.get("text"):
                        continue
                    try:
                        item_id = int(item.get("id", self._next_memo_id))
                    except (TypeError, ValueError):
                        item_id = self._next_memo_id
                    item["id"] = item_id
                    self._memos.append(item)
                    self._next_memo_id = max(self._next_memo_id, item_id + 1)
                try:
                    self._next_memo_id = max(self._next_memo_id, int(data.get("next_id", 1)))
                except (TypeError, ValueError):
                    pass
            except (OSError, ValueError, TypeError) as e:
                logger.warning(f"加载备忘失败: {e}")

        self._expire_memos(save=False)
        if self._journal or self._core or self._memos:
            logger.info(
                f"已加载记忆：core={len(self._core)} 字，"
                f"journal={len(self._journal)} 条，memo={len(self._memos)} 条"
            )

    def _write_text(self, path: Path, text: str):
        """原子写入（临时文件 + rename）；Windows 瞬态锁时短暂重试。"""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(3):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    tmp.unlink(missing_ok=True)
                    raise

    def _save_core(self):
        self._write_text(self.core_file, self._core)

    def _save_journal(self):
        lines = [json.dumps(e, ensure_ascii=False) for e in self._journal]
        self._write_text(self.journal_file, "\n".join(lines) + ("\n" if lines else ""))

    def _save_memo(self):
        data = {"next_id": self._next_memo_id, "items": self._memos}
        self._write_text(self.memo_file, json.dumps(data, ensure_ascii=False, indent=2))

    # ---------- 长期记忆 ----------
    def get_core(self) -> str:
        return self._core

    def set_core(self, text: str) -> bool:
        """全量替换长期记忆（仅记忆整理时调用）。

        Returns:
            True=已更新；False=文本为空/未启用
        """
        if not self.enabled:
            return False
        text = " ".join(str(text).split()).strip()
        if not text:
            return False
        text = text[: self.cfg.core_max_chars]
        changed = text != self._core
        self._core = text
        if changed:
            self._save_core()
        return changed

    # ---------- 近期日记 ----------
    def add_journal(self, text: str) -> Optional[dict]:
        """追加一条日记（自动生成递增 id）。"""
        if not self.enabled:
            return None
        text = " ".join(str(text).split()).strip()
        if not text:
            return None
        text = text[: self.cfg.journal_entry_max_chars]
        now = datetime.now()
        entry = {
            "id": self._next_journal_id,
            "created_at": time.time(),
            "time": now.strftime("%m-%d %H:%M"),
            "text": text,
        }
        self._next_journal_id += 1
        self._journal.append(entry)
        # 追加写：只写一行，避免每次重写整个文件
        with open(self.journal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(f"新增日记 #{entry['id']}: {text}")
        return entry

    def render_journal_block(self) -> str:
        """渲染注入 user content 的近期日记（按时间顺序，只取时间窗内最近 N 条）。"""
        if not self.enabled:
            return ""
        cutoff = time.time() - self.cfg.recent_journal_hours * 3600
        entries = [e for e in self._journal if e.get("created_at", 0) >= cutoff]
        entries = entries[-max(1, self.cfg.recent_journal_max_entries):]
        return "\n".join(f"[#{e['id']}] {e.get('time', '')} {e['text']}" for e in entries)

    def render_journal_for_consolidation(self) -> str:
        """渲染记忆整理时看到的全部日记（最多保留上限条）。"""
        if not self.enabled:
            return ""
        entries = self._journal[-max(1, self.cfg.journal_keep_max_entries):]
        return "\n".join(f"[#{e['id']}] {e.get('time', '')} {e['text']}" for e in entries)

    def prune_journal(self) -> int:
        """整理完成后淘汰旧日记（按时间窗 + 条数上限）。

        Returns:
            删除的条数
        """
        if not self.enabled:
            return 0
        cutoff = time.time() - self.cfg.journal_keep_hours * 3600
        kept = [e for e in self._journal if e.get("created_at", 0) >= cutoff]
        kept = kept[-max(1, self.cfg.journal_keep_max_entries):]
        removed = len(self._journal) - len(kept)
        if removed:
            self._journal = kept
            self._save_journal()
            logger.info(f"记忆整理：淘汰 {removed} 条旧日记，保留 {len(kept)} 条")
        return removed

    def has_journal(self) -> bool:
        return bool(self._journal)

    # ---------- 短期备忘 ----------
    def _expire_memos(self, save: bool = True) -> int:
        """删除过期备忘。"""
        if not self.memo_enabled:
            return 0
        now = datetime.now()
        kept, removed = [], 0
        for item in self._memos:
            expires_at = item.get("expires_at")
            if not expires_at:
                kept.append(item)
                continue
            try:
                expired = datetime.fromisoformat(str(expires_at)) <= now
            except (ValueError, TypeError):
                expired = False
            if expired:
                removed += 1
            else:
                kept.append(item)
        if removed:
            self._memos = kept
            if save:
                self._save_memo()
            logger.info(f"备忘过期：删除 {removed} 条，剩余 {len(kept)} 条")
        return removed

    def add_memo(self, text: str) -> Optional[dict]:
        """新增一条短期备忘（超过上限时淘汰最旧的一条）。"""
        if not self.memo_enabled:
            return None
        self._expire_memos()
        text = " ".join(str(text).split()).strip()
        if not text:
            return None
        text = text[: self.cfg.journal_entry_max_chars]
        if len(self._memos) >= self.cfg.memo_max_items:
            dropped = self._memos.pop(0)
            logger.info(f"备忘已满，淘汰最旧一条 #{dropped.get('id')}: {dropped.get('text', '')}")
        now = datetime.now()
        expires_at = now + timedelta(hours=self.cfg.memo_default_ttl_hours)
        item = {
            "id": self._next_memo_id,
            "text": text,
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }
        self._next_memo_id += 1
        self._memos.append(item)
        self._save_memo()
        logger.info(f"新增备忘 #{item['id']}: {text}（{self.cfg.memo_default_ttl_hours}h 后过期）")
        return item

    def remove_memos(self, ids: list) -> int:
        """按编号删除备忘。"""
        if not self.memo_enabled:
            return 0
        target = set()
        for x in ids:
            try:
                target.add(int(x))
            except (TypeError, ValueError):
                continue
        if not target:
            return 0
        kept = []
        removed = 0
        for m in self._memos:
            try:
                memo_id = int(m.get("id", 0))
            except (TypeError, ValueError):
                memo_id = -1
            if memo_id in target:
                removed += 1
            else:
                kept.append(m)
        if removed:
            self._memos = kept
            self._save_memo()
            logger.info(f"删除备忘 {removed} 条：{sorted(target)}")
        return removed

    def render_memo_block(self) -> str:
        """渲染注入 user content 的备忘列表。"""
        if not self.memo_enabled:
            return ""
        self._expire_memos()
        lines = []
        for item in self._memos:
            created = item.get("created_at", "")
            try:
                created_display = datetime.fromisoformat(str(created)).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                created_display = ""
            suffix = f"（{created_display} 记）" if created_display else ""
            lines.append(f"[#{item['id']}] {item['text']}{suffix}")
        return "\n".join(lines)
