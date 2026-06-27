"""历史记录管理（多轮对话格式）。

存储结构：messages 列表，严格 user/assistant 交替。
- user：每轮"看一眼"触发时的群消息批次
- assistant：LLM 返回的完整 JSON（含 thought，无论 action 是什么）

每轮 LLM 调用必有 assistant 落地，silent 也是真实回复。
超过阈值时对早期 user/assistant 对做分层压缩（方案A）：
- 近期 N 轮：保留原文
- 中期 M 轮：LLM 摘要（保留互动/话题/立场/情绪）
- 更早：朴素摘要（截断要点）拼入 summary 字段

并发模型：
- 接收线程（webhook）调 append_group_message 写 fast_buffer（_buffer_lock 保护）
- LLM 工作线程发送 bot 回复后也调 append_group_message(is_bot=True) 写 fast_buffer
  （与接收线程共用入口，按真实发言顺序与群消息混合在 fast_buffer 中）
- LLM 工作线程调 drain_buffer_to_pending 原子地把 fast_buffer 移到 pending
- pending 的所有后续读写都在 LLM 线程内，无需额外锁
- recent_message_count 需同时看 fast_buffer 和 pending，用 _buffer_lock 保护
  （并过滤 is_bot=True，避免 bot 自我催化话题热度评分）

压缩的并发安全（方案A）：
- 压缩在 LLM 工作线程的 append_turn 末尾执行（单线程，不引入新并发）
- 压缩期间 fast_buffer 仍可接收消息（_buffer_lock 保护，与压缩互不干扰）
- 压缩期间 pending 不被读写（drain 已完成，consume 已完成）
- 摘要调用 LLM 约 3-5 秒，期间阻塞 cycle，但下一轮 cycle 才用到新 messages，无竞态
- 持久化用临时文件+rename 原子写入，避免崩溃导致文件损坏
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

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
        # 包含中期 LLM 摘要 + 远期朴素摘要，按时间倒序拼接（最近摘要在前）
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
        # 摘要器：由 main.py 注入 LLMClient.summarize，None 时走朴素摘要
        self._summarizer: Optional[Callable[[list[dict]], Optional[str]]] = None
        self._load()

    def set_summarizer(self, summarizer: Callable[[list[dict]], Optional[str]]):
        """注入 LLM 摘要器（main.py 启动时调用）。

        Args:
            summarizer: 接收 messages 列表，返回摘要字符串或 None（失败）
        """
        self._summarizer = summarizer
        logger.info("已注入 LLM 摘要器，分层压缩启用（方案A）")

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
        """原子写入：临时文件 + rename，避免压缩中途崩溃导致文件损坏。"""
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({
                "messages": self.messages,
                "summary": self.summary,
                "pending_group_msgs": self.pending_group_msgs,
                "delayed_replies": self.delayed_replies,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.file)  # 原子操作（同文件系统内）

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

    def append_recall_notice(self, recalled_msg_id: str, operator_qq: str):
        """追加撤回通知伪消息到 fast_buffer（接收线程调用）。

        伪消息格式与普通群消息一致，nickname="系统"，content 标注被撤回的 msg_id。
        msg_id 为空（不可引用，build_user_content 不加 [#] 前缀），is_bot=False（让 LLM 看到这条通知）。
        LLM 通过 content 文本"msg_id=xxx 的消息被撤回"识别撤回事件（persona 规则 8.5 引导）。
        下一轮 drain 时进入 pending。

        线程安全：持 _buffer_lock，与 drain 操作不冲突。
        """
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "qq": operator_qq,
            "nickname": "系统",
            "content": f"msg_id={recalled_msg_id} 的消息被撤回",
            "is_bot": False,
            "msg_id": "",  # 伪消息无 msg_id，不可引用
        }
        with self._buffer_lock:
            self.fast_buffer.append(entry)
        logger.info(f"撤回通知入 fast_buffer: recalled_msg_id={recalled_msg_id} operator={operator_qq}")

    def append_poke_notice(self, poker_qq: str, poker_nickname: str):
        """追加戳一戳通知伪消息到 fast_buffer（接收线程调用）。

        伪消息用戳人者的昵称，content="戳了戳我"，msg_id 为空（不可引用）。
        LLM 看到形如 "[time] 张三(123): 戳了戳我"，自然理解为被戳。
        下一轮 drain 时进入 pending。

        线程安全：持 _buffer_lock，与 drain 操作不冲突。
        """
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "qq": poker_qq,
            "nickname": poker_nickname,
            "content": "戳了戳我",
            "is_bot": False,
            "msg_id": "",  # 伪消息无 msg_id，不可引用
        }
        with self._buffer_lock:
            self.fast_buffer.append(entry)
        logger.info(f"戳一戳通知入 fast_buffer: poker={poker_nickname}({poker_qq})")

    def update_group_message_content(self, msg_id: str, new_content: str) -> bool:
        """按 msg_id 更新群消息的 content（语音转写回填用）。

        遍历 fast_buffer 和 pending_group_msgs（消息被 LLM 消费进 messages 后无法回填）。
        线程安全：fast_buffer 加 _buffer_lock，pending_group_msgs 是 list 引用操作。

        Returns:
            True: 找到并更新；False: 未找到（可能已被 drain+消费）
        """
        updated = False
        with self._buffer_lock:
            for entry in self.fast_buffer:
                if entry.get("msg_id") == msg_id:
                    entry["content"] = new_content
                    updated = True
                    break
        # pending_group_msgs 的写操作主要由 LLM 工作线程的 drain/consume 触发，
        # 这里只做单条 content 赋值（dict 引用），与 drain 的 list extend 不冲突
        for entry in self.pending_group_msgs:
            if entry.get("msg_id") == msg_id:
                entry["content"] = new_content
                updated = True
                break
        if updated:
            self._save()
        return updated

    # ---------- 构建 user content（触发"看一眼"时调用） ----------
    def build_user_content(self) -> str:
        """把 pending buffer 拼成 user content。

        格式（msg_id 标记用于 reply 段引用，bot 消息无标记不可引用）：
        # 最近群消息（按时间顺序，每行一条，[#msg_id] 标记可用于 reply 段引用）
        [#1281341473] [20:03:37] 张三(123456789): 你好啊
        [#1281341474] [20:03:43] 张三(123456789): 你好
        [20:04:00] [bot] 林夏(...): 嗯                    # bot 消息不可引用（无 [#] 前缀）
        [#1281341475] [20:04:05] 张三(123456789): 之前那条  [延迟回复]

        注：连发由"批量 drain + 静默窗口"自然体现——LLM 会看到多条同一发送者
        时间戳相邻的消息，无需额外标记。
        """
        lines = []
        for m in self.pending_group_msgs:
            prefix = "[bot]" if m["is_bot"] else ""
            msg_id_tag = f"[#{m['msg_id']}]" if (not m["is_bot"] and m.get("msg_id")) else ""
            tag = "  [延迟回复]" if m.get("is_delayed") else ""
            lines.append(
                f"{msg_id_tag} [{m['time']}] {prefix}{m['nickname']}({m['qq']}): {m['content']}{tag}"
            )
        return "\n".join(lines)

    def consume_pending_into_user(self):
        """清空 pending（user_content 已由调用方通过 build_user_content 预构建）。

        B2 方案：build_user_content 在 LLM 调用前执行（渲染文本），
        consume 在 LLM 调用后执行（只清空 pending，不重新 build）。
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
        """轮次达阈值时，对早期 user/assistant 对做分层压缩（方案A）。

        分层结构：
        - 近期 history_keep_recent / 2 轮：保留原文
        - 中期 history_keep_mid / 2 轮：LLM 摘要（调用注入的 summarizer）
        - 更早：朴素摘要（截断要点）拼入 summary 字段

        触发条件：user/assistant 对数 >= history_limit / 2。
        容错：LLM 摘要失败则放弃本次压缩，下次 append_turn 再试。

        并发安全：本方法在 LLM 工作线程的 append_turn 末尾执行，单线程无竞态。
        摘要调用 LLM 约 3-5 秒，期间 fast_buffer 仍可接收消息，pending 不被读写。
        """
        turn_count = len(self.messages) // 2
        max_turns = self.config.history_limit // 2
        if turn_count < max_turns:
            return

        keep_turns = self.config.history_keep_recent // 2
        mid_turns = self.config.history_keep_mid // 2

        keep_msgs = keep_turns * 2
        mid_msgs = mid_turns * 2

        # 切分：[远期 old][中期 mid][近期 keep]
        # 近期保留，中期 LLM 摘要，远期朴素摘要
        old_msgs = self.messages[:-(keep_msgs + mid_msgs)]
        mid_msgs_list = self.messages[-(keep_msgs + mid_msgs):-keep_msgs] if mid_msgs > 0 else []

        # 1. 中期 LLM 摘要（若配置了 summarizer 且有中期消息）
        mid_summary = ""
        if mid_msgs_list and self._summarizer:
            logger.info(f"分层压缩：对中期 {mid_turns} 轮调用 LLM 摘要...")
            mid_summary = self._summarizer(mid_msgs_list) or ""
            if not mid_summary:
                # LLM 摘要失败，放弃本次压缩，下次重试
                logger.warning("LLM 摘要失败，放弃本次压缩（下次 append_turn 再试）")
                return

        # 2. 远期朴素摘要（截断保留要点）
        old_summary_chunk = ""
        if old_msgs:
            old_text_parts = []
            for i in range(0, len(old_msgs), 2):
                if i + 1 < len(old_msgs):
                    # user content 取后 100 字（跳过群成员列表头部，取消息正文）
                    u = old_msgs[i]["content"][-100:]
                    a = old_msgs[i + 1]["content"][:100]
                    old_text_parts.append(f"U:{u}\nA:{a}")
            old_summary_chunk = " || ".join(old_text_parts[-5:])

        # 3. 合并 summary：新中期摘要在前 + 已有 summary + 新远期摘要
        #    结构：[最近的中期摘要] [更早的中期/远期摘要] [本次远期摘要]
        #    summary 限 2000 字（容纳多轮压缩的累积）
        parts = []
        if mid_summary:
            parts.append(mid_summary)
        if self.summary:
            parts.append(self.summary)
        if old_summary_chunk:
            parts.append(old_summary_chunk)
        self.summary = " ".join(parts).strip()[-2000:]

        # 4. 切除中期和远期，只保留近期原文
        self.messages = self.messages[-keep_msgs:]

        logger.info(
            f"分层压缩完成：保留近 {keep_turns} 轮原文"
            f"{f'，中期摘要 {len(mid_summary)} 字' if mid_summary else ''}"
            f"{f'，远期朴素摘要 {len(old_summary_chunk)} 字' if old_summary_chunk else ''}"
            f"，summary 总长 {len(self.summary)} 字"
        )

    # ---------- 查询 ----------
    def get_messages_for_llm(self) -> list[dict]:
        """返回传给 LLM 的 messages 列表（不含 system，system 由调用方拼接）。"""
        return self.messages.copy()

    def get_msg_id_by_id(self, target_msg_id: str) -> Optional[str]:
        """校验 target_msg_id 是否可引用（用于 reply / react 段）。

        新方案：msg_id 由 LLM 直接输出（从 user content 的 [#msg_id] 标记复制），
        无需 index→msg_id 反查映射。本方法只做空值校验，有效性交给 NapCat。

        Args:
            target_msg_id: LLM 输出的 msg_id 字符串

        Returns:
            非空 msg_id 字符串（透传，由 NapCat 做最终校验）；
            空串或无效返回 None，sender 会跳过 reply 段。
        """
        if not target_msg_id or not isinstance(target_msg_id, str):
            return None
        return target_msg_id

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
