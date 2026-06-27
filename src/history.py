"""历史记录管理（多轮对话格式）。

存储结构：messages 列表，严格 user/assistant 交替。
- user：每轮"看一眼"触发时的群消息批次
- assistant：LLM 返回的完整 JSON（含 thought，无论 action 是什么）

每轮 LLM 调用必有 assistant 落地，silent 也是真实回复。
超过阈值时对早期 user/assistant 对做摘要压缩，塞进 system 末尾。

并发模型：
- 接收线程（webhook）调 append_group_message 写 fast_buffer（_buffer_lock 保护）
- LLM 工作线程发送 bot 回复后也调 append_group_message(is_bot=True) 写 fast_buffer
  （与接收线程共用入口，按真实发言顺序与群消息混合在 fast_buffer 中）
- LLM 工作线程调 drain_buffer_to_pending 原子地把 fast_buffer 移到 pending
- pending 的所有后续读写都在 LLM 线程内，无需额外锁
- recent_message_count 需同时看 fast_buffer 和 pending，用 _buffer_lock 保护
  （并过滤 is_bot=True，避免 bot 自我催化话题热度评分）
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import TriggerConfig
from .utils.logger import get_logger

logger = get_logger("history")


class HistoryManager:
    """多轮对话历史管理器。"""

    def __init__(self, config: TriggerConfig, state_dir: str = "state"):
        self.config = config
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(exist_ok=True)
        self.file = self.state_dir / "conversation.json"
        # messages: [{"role":"user","content":"..."}, {"role":"assistant","content":"...JSON..."}, ...]
        self.messages: list[dict] = []
        # 早期对话摘要（压缩后塞进 system 末尾）
        self.summary: str = ""
        # 待拼入下一个 user 的群消息缓冲（未触发"看一眼"的消息累积于此）
        # 格式：[{"time":"HH:MM","qq":"...","nickname":"...","content":"...","is_bot":bool,"msg_id":"..."}]
        self.pending_group_msgs: list[dict] = []
        # fast_buffer：接收线程无锁快速写入，LLM 线程 drain 到 pending
        # 解决"LLM 调用持锁期间新消息无法 pending"的问题
        self.fast_buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        # 延迟回复缓冲：LLM 觉得"等会再回"时，把这批 pending 消息存到这里
        # 格式：[{"due_time":"ISO时间戳", "messages":[{同 pending 格式}]}]
        self.delayed_replies: list[dict] = []
        # 渲染快照：build_user_content 时对 pending 做浅拷贝，供后续 get_msg_id_by_index 读取。
        # 解决"consume_pending_into_user 清空 pending 后，_handle_result 中 reply/react 段取不到 msg_id"的问题。
        # 快照在 LLM 工作线程内创建和消费，无跨线程访问，无需额外锁。
        self._rendered_pending_snapshot: list[dict] = []
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                self.messages = data.get("messages", [])
                self.summary = data.get("summary", "")
                self.pending_group_msgs = data.get("pending_group_msgs", [])
                self.delayed_replies = data.get("delayed_replies", [])
            except Exception as e:
                logger.warning(f"加载历史失败: {e}")

    def _save(self):
        self.file.write_text(
            json.dumps({
                "messages": self.messages,
                "summary": self.summary,
                "pending_group_msgs": self.pending_group_msgs,
                "delayed_replies": self.delayed_replies,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- 群消息追加（入 fast_buffer，接收线程 / LLM 线程均调用） ----------
    def append_group_message(self, qq: str, nickname: str, content: str, msg_id: str, is_bot: bool = False):
        """追加群消息到 fast_buffer（接收线程与 LLM 线程共用入口）。

        只加 _buffer_lock，持锁时间极短（仅 list.append）。
        消息不会立即进入 pending，要等 LLM 工作线程 drain_buffer_to_pending。

        bot 自身回复（is_bot=True）也走此入口，由 LLM 工作线程在发送后调用，
        与群成员消息按真实 append 顺序混合在 fast_buffer 中，下一轮 drain 时
        一起进入 pending，保证 LLM 看到的发言顺序 = 真实群聊发言顺序。
        """
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "qq": qq,
            "nickname": nickname,
            "content": content,
            "is_bot": is_bot,
            "msg_id": msg_id,
        }
        with self._buffer_lock:
            self.fast_buffer.append(entry)

    def drain_buffer_to_pending(self) -> int:
        """把 fast_buffer 原子地移到 pending（LLM 工作线程调用）。

        Returns:
            本次 drain 的消息条数
        """
        with self._buffer_lock:
            if not self.fast_buffer:
                return 0
            drained = self.fast_buffer
            self.fast_buffer = []
        self.pending_group_msgs.extend(drained)
        self._save()
        return len(drained)

    # ---------- 构建 user content（触发"看一眼"时调用） ----------
    def build_user_content(self) -> str:
        """把 pending buffer 拼成 user content。

        同时做一份 pending 浅拷贝存到 _rendered_pending_snapshot，
        供后续 get_msg_id_by_index 读取（consume 清空 pending 后仍可查 msg_id）。

        格式（正序 1-based 编号，用于 reply 段的 target_msg_index）：
        # 最近群消息（按时间顺序，每行一条，编号可用于 reply 段引用）
        [1] [20:03:37] 张三(123456789): 你好啊
        [2] [20:03:43] 张三(123456789): 你好
        [3] [20:04:00] [bot] 林夏(...): 嗯                # bot 消息不可引用
        [4] [20:04:05] 张三(123456789): 之前那条          [延迟回复]

        注：连发由"批量 drain + 静默窗口"自然体现——LLM 会看到多条同一发送者
        时间戳相邻的消息，无需额外标记。
        """
        # 快照：LLM 看到的 [N] 编号 ↔ msg_id 映射，供 reply/react 段解析引用
        self._rendered_pending_snapshot = list(self.pending_group_msgs)
        lines = []
        for i, m in enumerate(self.pending_group_msgs):
            seq = i + 1  # 正序 1-based
            prefix = "[bot]" if m["is_bot"] else ""
            tag = "  [延迟回复]" if m.get("is_delayed") else ""
            lines.append(
                f"[{seq}] [{m['time']}] {prefix}{m['nickname']}({m['qq']}): {m['content']}{tag}"
            )
        return "\n".join(lines)

    def consume_pending_into_user(self):
        """清空 pending（user_content 已由调用方通过 build_user_content 预构建）。

        B2 方案：build_user_content 在 LLM 调用前执行（创建快照+渲染文本），
        consume 在 LLM 调用后执行（只清空 pending，不重新 build）。
        避免重复 build，且保证 reply/react 段引用的快照与本轮 user_content 一致。
        """
        self.pending_group_msgs = []

    # ---------- 落地一轮对话 ----------
    def append_turn(self, user_content: str, assistant_content: str):
        """追加一轮 user/assistant 对话。

        Args:
            user_content: 本轮群消息批次
            assistant_content: LLM 返回的完整 JSON 字符串
        """
        self.messages.append({"role": "user", "content": user_content})
        self.messages.append({"role": "assistant", "content": assistant_content})
        self._check_compress()
        self._save()

    # ---------- 摘要压缩 ----------
    def _check_compress(self):
        """轮次达阈值时，对早期 user/assistant 对做摘要压缩。

        触发条件：user/assistant 对数 >= history_limit / 2。
        压缩策略：保留近 history_keep_recent / 2 轮原文，早期转摘要塞进 system 末尾。
        当前阶段摘要为朴素实现（截断保留要点），阶段二接 LLM 做真实摘要。
        """
        turn_count = len(self.messages) // 2
        max_turns = self.config.history_limit // 2
        if turn_count < max_turns:
            return

        keep_turns = self.config.history_keep_recent // 2
        keep_msgs = keep_turns * 2
        old_msgs = self.messages[:-keep_msgs]
        self.messages = self.messages[-keep_msgs:]

        # 朴素摘要：把早期对话拼成文本（阶段二改为调用 LLM 摘要）
        old_text_parts = []
        for i in range(0, len(old_msgs), 2):
            if i + 1 < len(old_msgs):
                u = old_msgs[i]["content"][:100]
                a = old_msgs[i + 1]["content"][:100]
                old_text_parts.append(f"U:{u}\nA:{a}")
        old_summary_chunk = " || ".join(old_text_parts[-5:])
        self.summary = (self.summary + " " + old_summary_chunk).strip()[-800:]
        logger.info(f"对话压缩：保留近 {keep_turns} 轮，摘要 {len(self.summary)} 字")

    # ---------- 查询 ----------
    def get_messages_for_llm(self) -> list[dict]:
        """返回传给 LLM 的 messages 列表（不含 system，system 由调用方拼接）。"""
        return self.messages.copy()

    def get_msg_id_by_index(self, index: int) -> Optional[str]:
        """按 user content 中的正序编号取 msg_id（用于 reply / react 段）。

        index 含义：1=第一条群消息，2=第二条...（与 user content 显示的 [N] 编号一致）
        bot 消息不可引用（返回 None）。
        越界或无效返回 None，sender 会跳过 reply 段。

        注意：读 _rendered_pending_snapshot 而非 live pending——
        本方法在 consume_pending_into_user 之后调用（_handle_result 阶段），
        此时 pending 已清空，只有快照保留着 LLM 看到的编号→msg_id 映射。
        """
        snapshot = self._rendered_pending_snapshot
        if index < 1 or index > len(snapshot):
            return None
        m = snapshot[index - 1]
        if m["is_bot"]:
            return None  # 不允许引用 bot 自己的消息
        return m.get("msg_id", "") or None

    def recent_message_count(self, seconds: int = 180) -> int:
        """最近 N 秒内群消息数量（用于话题热度评分）。

        同时考虑 fast_buffer + pending（接收线程调用时 pending 可能正被 LLM 线程读写，
        故用 _buffer_lock 保护快照）。
        兼容旧格式 HH:MM 和新格式 HH:MM:SS。
        """
        now = datetime.now()
        # 快照 fast_buffer（与 pending 拼接），避免遍历过程中被修改
        with self._buffer_lock:
            snapshot = list(self.fast_buffer)
        # pending 部分（LLM 线程读写时这里可能读到旧值，可接受——评分只用于粗筛）
        combined = snapshot + self.pending_group_msgs

        count = 0
        for m in reversed(combined):
            # 过滤 bot 自身回复：话题热度应反映群成员活跃度，
            # 否则 bot 回复会自我催化触发（bot 刚说完就 +1 热度）
            if m.get("is_bot"):
                continue
            try:
                time_str = m['time']
                if time_str.count(":") == 1:
                    msg_time = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
                else:
                    msg_time = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M:%S")
                if (now - msg_time).total_seconds() <= seconds:
                    count += 1
                else:
                    break
            except ValueError:
                continue

        return count

    def get_summary(self) -> str:
        """返回早期对话摘要（拼到 system 末尾用）。"""
        return self.summary

    # ---------- 延迟回复管理 ----------
    def stash_pending_as_delayed(self, delay_minutes: int) -> int:
        """把当前 pending 消息存到 delayed_replies，N 分钟后到期。

        用于 LLM 输出 reply_delay_minutes 时：当前这批消息暂不回，
        等 N 分钟后由下次触发重新带入 pending。

        Args:
            delay_minutes: 延迟分钟数（LLM 输出）

        Returns:
            存入的消息条数
        """
        if not self.pending_group_msgs:
            return 0
        from datetime import timedelta
        due = datetime.now() + timedelta(minutes=delay_minutes)
        self.delayed_replies.append({
            "due_time": due.isoformat(),
            "messages": list(self.pending_group_msgs),  # 拷贝
        })
        count = len(self.pending_group_msgs)
        logger.info(f"延迟回复：存入 {count} 条消息，{delay_minutes} 分钟后到期（{due.strftime('%H:%M:%S')}）")
        self._save()
        return count

    def pop_due_delayed_into_pending(self) -> int:
        """检查 delayed_replies，把到期/超时的消息重新加入 pending。

        在每次 LLM 调用前调用。到期的延迟消息会以 [延迟回复] 标注加入 pending，
        触发 LLM 的 multi_reply 分片回复。

        Returns:
            重新加入 pending 的消息条数
        """
        if not self.delayed_replies:
            return 0

        now = datetime.now()
        due_items = []
        remaining = []
        for item in self.delayed_replies:
            try:
                due_time = datetime.fromisoformat(item["due_time"])
                if due_time <= now:
                    due_items.append(item)
                else:
                    remaining.append(item)
            except (ValueError, KeyError):
                # 解析失败的也视为到期（避免永久卡住）
                due_items.append(item)

        if not due_items:
            return 0

        # 把到期消息加入 pending，标注 is_delayed=True
        added = 0
        for item in due_items:
            for m in item["messages"]:
                # 标注为延迟回复（render 时会显示 [延迟回复]）
                m_copy = dict(m)
                m_copy["is_delayed"] = True
                # 更新时间为当前时间（避免旧时间戳让 LLM 误判消息间隔）
                m_copy["time"] = now.strftime("%H:%M:%S")
                self.pending_group_msgs.append(m_copy)
                added += 1

        self.delayed_replies = remaining
        if added > 0:
            logger.info(f"延迟回复到期：{added} 条消息重新加入 pending")
            self._save()
        return added

    def has_delayed_replies(self) -> bool:
        """是否有未到期的延迟回复。"""
        return len(self.delayed_replies) > 0
