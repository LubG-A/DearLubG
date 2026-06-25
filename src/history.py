"""历史记录管理（多轮对话格式）。

存储结构：messages 列表，严格 user/assistant 交替。
- user：每轮"看一眼"触发时的群消息批次
- assistant：LLM 返回的完整 JSON（含 thought，无论 action 是什么）

每轮 LLM 调用必有 assistant 落地，silent 也是真实回复。
超过阈值时对早期 user/assistant 对做摘要压缩，塞进 system 末尾。
"""
import json
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
        # 延迟回复缓冲：LLM 觉得"等会再回"时，把这批 pending 消息存到这里
        # 格式：[{"due_time":"ISO时间戳", "messages":[{同 pending 格式}]}]
        self.delayed_replies: list[dict] = []
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

    # ---------- 群消息追加（入 pending buffer） ----------
    def append_group_message(self, qq: str, nickname: str, content: str, msg_id: str):
        """追加群消息到 pending buffer（等待下一次"看一眼"触发时拼入 user）。"""
        self.pending_group_msgs.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "qq": qq,
            "nickname": nickname,
            "content": content,
            "is_bot": False,
            "msg_id": msg_id,
        })
        self._save()

    def append_bot_reply_to_pending(self, qq: str, nickname: str, content: str):
        """机器人自身回复也追加到 pending（写回历史形成闭环，但待下次触发时才进入 user）。"""
        self.pending_group_msgs.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "qq": qq,
            "nickname": nickname,
            "content": content,
            "is_bot": True,
            "msg_id": "",
        })
        self._save()

    # ---------- 连发消息检测 ----------
    # 同一发送者在 BURST_INTERVAL_SECONDS 内连续发多条消息 → 视为"连发中"
    # 在 render_user_content 时给这些消息打 [连发中] 标记，配合提示词引导 LLM silent
    BURST_INTERVAL_SECONDS = 15  # 连发判定时间窗口（秒）

    def mark_burst_messages(self) -> set:
        """检测 pending 中"连发中"的消息，返回需要标记的 message index 集合。

        判定规则：同一发送者相邻两条消息间隔 ≤ BURST_INTERVAL_SECONDS，
        则这两条（以及前后连续的）都算连发。bot 自身回复不参与连发判定。

        Returns:
            set of int：需要标记 [连发中] 的 pending_group_msgs 索引集合
        """
        if len(self.pending_group_msgs) < 2:
            return set()

        marked = set()
        # 按发送者分组相邻消息，检查时间间隔
        # 只对非 bot 消息做连发判断
        non_bot_indices = [i for i, m in enumerate(self.pending_group_msgs) if not m["is_bot"]]
        if len(non_bot_indices) < 2:
            return set()

        # 对每个发送者，扫描其连续消息
        # 策略：用滑动窗口，若相邻两条（同一发送者、时间间隔 ≤ 阈值）则都标记
        # 一旦断裂（不同人 or 间隔 > 阈值），结束当前连发段
        burst_chain = []  # 当前连发段的 pending 索引列表

        for i in non_bot_indices:
            if not burst_chain:
                burst_chain = [i]
                continue

            prev_i = burst_chain[-1]
            prev_msg = self.pending_group_msgs[prev_i]
            cur_msg = self.pending_group_msgs[i]

            same_sender = (prev_msg["qq"] == cur_msg["qq"])
            interval_ok = self._time_interval_seconds(prev_msg["time"], cur_msg["time"]) <= self.BURST_INTERVAL_SECONDS

            if same_sender and interval_ok:
                burst_chain.append(i)
            else:
                # 连发段断裂，结算上一段
                if len(burst_chain) >= 2:
                    marked.update(burst_chain)
                burst_chain = [i]

        # 结算最后一段
        if len(burst_chain) >= 2:
            marked.update(burst_chain)

        return marked

    @staticmethod
    def _time_interval_seconds(t1: str, t2: str) -> int:
        """计算 HH:MM:SS 格式两个时间的间隔秒数（绝对值）。"""
        try:
            h1, m1, s1 = map(int, t1.split(":"))
            h2, m2, s2 = map(int, t2.split(":"))
            sec1 = h1 * 3600 + m1 * 60 + s1
            sec2 = h2 * 3600 + m2 * 60 + s2
            return abs(sec2 - sec1)
        except (ValueError, AttributeError):
            # 旧格式 HH:MM 兼容：fallback 到分钟级精度
            try:
                h1, m1 = map(int, str(t1).split(":")[:2])
                h2, m2 = map(int, str(t2).split(":")[:2])
                return abs((h2 * 60 + m2) - (h1 * 60 + m1)) * 60
            except Exception:
                return 9999  # 解析失败，视为超长间隔（不连发）

    # ---------- 构建 user content（触发"看一眼"时调用） ----------
    def build_user_content(self) -> str:
        """把 pending buffer 拼成 user content，并清空 buffer。

        格式（正序 1-based 编号，用于 reply 段的 target_msg_index）：
        # 最近群消息（按时间顺序，每行一条，编号可用于 reply 段引用）
        [1] [20:03:37] 张三(123456789): 你好啊
        [2] [20:03:43] 张三(123456789): 你好              [连发中]
        [3] [20:04:00] [bot] 林夏(...): 嗯                # bot 消息不可引用
        [4] [20:04:05] 张三(123456789): 之前那条          [延迟回复]
        """
        burst_indices = self.mark_burst_messages()
        lines = []
        for i, m in enumerate(self.pending_group_msgs):
            seq = i + 1  # 正序 1-based
            prefix = "[bot]" if m["is_bot"] else ""
            tags = []
            if i in burst_indices:
                tags.append("[连发中]")
            if m.get("is_delayed"):
                tags.append("[延迟回复]")
            tag_str = ("  " + " ".join(tags)) if tags else ""
            lines.append(
                f"[{seq}] [{m['time']}] {prefix}{m['nickname']}({m['qq']}): {m['content']}{tag_str}"
            )
        return "\n".join(lines)

    def consume_pending_into_user(self) -> str:
        """构建 user content 并把 pending 清空（用于落盘到 messages）。"""
        content = self.build_user_content()
        self.pending_group_msgs = []
        return content

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
        """按 user content 中的正序编号取 msg_id（用于 reply 段）。

        index 含义：1=第一条群消息，2=第二条...（与 user content 显示的 [N] 编号一致）
        bot 消息不可引用（返回 None）。
        越界或无效返回 None，sender 会跳过 reply 段。
        """
        if index < 1 or index > len(self.pending_group_msgs):
            return None
        m = self.pending_group_msgs[index - 1]
        if m["is_bot"]:
            return None  # 不允许引用 bot 自己的消息
        return m.get("msg_id", "") or None

    def recent_message_count(self, seconds: int = 180) -> int:
        """最近 N 秒内群消息数量（用于话题热度评分）。

        同时考虑 pending 与已落地的 messages。
        兼容旧格式 HH:MM 和新格式 HH:MM:SS。
        """
        now = datetime.now()
        count = 0

        # pending 部分
        for m in reversed(self.pending_group_msgs):
            try:
                # 兼容 HH:MM 与 HH:MM:SS
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
                # 更新时间为当前时间（让连发检测等逻辑正常工作）
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
