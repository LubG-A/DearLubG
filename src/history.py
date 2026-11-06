"""历史记录管理（多轮对话格式）。

存储结构：messages 列表，严格 user/assistant 交替。
- user：每轮"看一眼"触发时的群消息批次
- assistant：LLM 返回的完整行指令输出（含 state 内心状态）

每轮 LLM 调用必有 assistant 落地，silent 也是真实回复。

长期记忆不在这里：core / journal / memo 由 MemoryManager（src/memory.py）管理。
本模块只负责"近期工作记忆"（messages + pending + delayed_replies）：
- 超过阈值时由 main.py 触发记忆整理；
- 整理产出记忆后，main.py 调 trim_to_recent 裁掉早期原文；
- 未产出记忆时不裁剪（失败保底，消息不丢失）。

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
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.state_dir / "conversation.json"
        # messages: [{"role":"user","content":"..."}, {"role":"assistant","content":"...行指令..."}, ...]
        self.messages: list[dict] = []
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
        # P006：最近对话块队列（尚未写入 messages 的最近 N 轮），随 conversation.json 持久化
        # 每块 = {entries, assistant, state, is_active, intent, created_at, finalized}
        self.recent_blocks: list[dict] = []
        self._blocks_lock = threading.Lock()
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                self.messages = data.get("messages", [])
                self.pending_group_msgs = data.get("pending_group_msgs", [])
                self.delayed_replies = data.get("delayed_replies", [])
                self.recent_blocks = self._sanitize_recent_blocks(data.get("recent_blocks", []))
            except Exception as e:
                logger.warning(f"加载历史失败: {e}")
        self._migrate_legacy_messages()

    @staticmethod
    def _sanitize_recent_blocks(raw) -> list[dict]:
        """校验并重建持久化的最近对话块，脏数据直接丢弃（避免启动时崩溃）。"""
        if not isinstance(raw, list):
            return []
        result = []
        for b in raw:
            if not isinstance(b, dict) or not b.get("finalized"):
                continue
            entries = b.get("entries")
            if not isinstance(entries, list):
                entries = []
            entries = [e for e in entries if isinstance(e, dict)]
            state = b.get("state")
            result.append({
                "entries": entries,
                "assistant": b.get("assistant") if isinstance(b.get("assistant"), str) else "",
                "state": state if isinstance(state, dict) else {},
                "is_active": bool(b.get("is_active", False)),
                "intent": b.get("intent") if isinstance(b.get("intent"), str) else "",
                "created_at": b.get("created_at") if isinstance(b.get("created_at"), str) else "",
                "finalized": True,
            })
        return result

    def _migrate_legacy_messages(self):
        """P005 迁移：旧 assistant 是 JSON，会教模型继续输出 JSON。

        检测到旧格式时归档原文件并清空 messages（pending/delayed 保留），
        避免新协议上线后模型整段模仿旧 JSON、被 parser 全部丢弃。
        """
        legacy = any(
            m.get("role") == "assistant" and str(m.get("content", "")).lstrip().startswith("{")
            for m in self.messages
        )
        if not legacy:
            return
        backup = self.file.with_name("conversation.legacy.json")
        try:
            backup.write_text(self.file.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as e:
            logger.warning(f"旧历史归档失败（继续清空内存）: {e}")
        self.messages = []
        try:
            self._save()
        except OSError as e:
            logger.warning(f"旧历史清理落盘失败（下次 append_turn 会覆盖）: {e}")
        logger.warning(f"检测到旧 JSON 历史，已归档到 {backup} 并清空 messages（P005 迁移）")

    def _save(self):
        """原子写入：临时文件 + rename，避免崩溃导致文件损坏。"""
        with self._blocks_lock:
            recent_blocks = list(self.recent_blocks)
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({
                "messages": self.messages,
                "pending_group_msgs": self.pending_group_msgs,
                "delayed_replies": self.delayed_replies,
                "recent_blocks": recent_blocks,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.file)  # 原子操作（同文件系统内）

    # ---------- P006：最近对话块队列 ----------
    def append_recent_block(self, block: dict, limit: int) -> Optional[dict]:
        """追加块；超过 limit 时 pop 最老块并返回（不落盘，调用方负责 save）。"""
        with self._blocks_lock:
            self.recent_blocks.append(block)
            if len(self.recent_blocks) > limit:
                return self.recent_blocks.pop(0)
            return None

    def take_recent_blocks(self) -> list:
        """取出并清空队列（不落盘，调用方负责 save）。"""
        with self._blocks_lock:
            blocks = list(self.recent_blocks)
            self.recent_blocks = []
        return blocks

    def get_recent_blocks(self) -> list:
        """只读快照（供成员列表/渲染使用）。"""
        with self._blocks_lock:
            return list(self.recent_blocks)

    def get_recent_block_entries(self) -> list:
        """按块顺序展开队列内的消息结构（供当前请求拼接）。"""
        result = []
        for b in self.get_recent_blocks():
            entries = b.get("entries")
            if isinstance(entries, list):
                result.extend(e for e in entries if isinstance(e, dict))
        return result

    def save(self):
        """显式落盘（P006 成功路径用于一次原子写入）。"""
        self._save()

    # ---------- 群消息追加（入 fast_buffer，接收线程 / LLM 线程均调用） ----------
    def append_group_message(self, qq: str, nickname: str, content: str, msg_id: str,
                             is_bot: bool = False, images: Optional[list[dict]] = None):
        """追加群消息到 fast_buffer（接收线程与 LLM 线程共用入口）。

        只加 _buffer_lock，持锁时间极短（仅 list.append）。
        消息不会立即进入 pending，要等 LLM 工作线程 drain_buffer_to_pending。

        bot 自身回复（is_bot=True）也走此入口，由 LLM 工作线程在发送后调用，
        与群成员消息按真实 append 顺序混合在 fast_buffer 中，下一轮 drain 时
        一起进入 pending，保证 LLM 看到的发言顺序 = 真实群聊发言顺序。

        Args:
            images: 图片源列表（NapCat image 段 data），供本轮识图使用；
                    只随当前 pending 批次进入 LLM 请求，历史 messages 只保留文本标记。
        """
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "qq": qq,
            "nickname": nickname,
            "content": content,
            "is_bot": is_bot,
            "msg_id": msg_id,
            "images": list(images) if images else [],
        }
        with self._buffer_lock:
            self.fast_buffer.append(entry)

    def has_unseen_buffered_messages(self) -> bool:
        """fast_buffer 中是否有尚未被 drain 的非 bot 消息。

        用于 cycle 结束时的补触发判定：drain 在 cycle 开头执行，所以此时仍留在
        fast_buffer 的群消息必然是 LLM 调用/发送期间到达、LLM 没看到过的。
        """
        with self._buffer_lock:
            return any(not m.get("is_bot") for m in self.fast_buffer)

    def collect_pending_images(self) -> list[dict]:
        """收集当前 pending 批次中的图片源（按消息顺序，忽略 bot 消息）。

        仅用于本轮 LLM 请求；历史 messages 只存文本（含 [图片] 标记），
        避免每轮重复发送 base64 图片导致请求体/token 膨胀。
        """
        images = []
        for m in self.pending_group_msgs:
            if m.get("is_bot"):
                continue
            for src in m.get("images", []):
                if isinstance(src, dict):
                    images.append(src)
        return images

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
        LLM 通过 content 文本"msg_id=xxx 的消息被撤回"识别撤回事件（persona 规则 10 引导）。
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
    def render_messages(self, entries: list) -> str:
        """把消息结构列表渲染成 user content 里的消息行（P006 也用于最近对话块）。

        格式（msg_id 标记用于 reply 段引用，bot 消息无标记不可引用）：
        [#1281341473] [20:03:37] 张三(123456789): 你好啊
        [#1281341474] [20:03:43] 张三(123456789): 你好
        [20:04:00] [bot] 林夏(...): 嗯                    # bot 消息不可引用（无 [#] 前缀）
        [#1281341475] [20:04:05] 张三(123456789): 之前那条  [延迟回复]
        """
        lines = []
        for m in (entries or []):
            if not isinstance(m, dict):
                continue
            prefix = "[bot]" if m.get("is_bot") else ""
            msg_id = m.get("msg_id", "")
            msg_id_tag = f"[#{msg_id}]" if (not m.get("is_bot") and msg_id) else ""
            tag = "  [延迟回复]" if m.get("is_delayed") else ""
            lines.append(
                f"{msg_id_tag} [{m.get('time', '')}] {prefix}"
                f"{m.get('nickname', '')}({m.get('qq', '')}): {m.get('content', '')}{tag}"
            )
        return "\n".join(lines)

    def build_user_content(self, prefix_entries: Optional[list] = None) -> str:
        """把"前置消息结构（最近对话块）+ 当前 pending"拼成 user content。

        复用原 build_user_content 的渲染逻辑（render_messages）：
        - prefix_entries 为空时等价于原来的"只渲染当前 pending"；
        - P006 传入最近对话块的 entries，让队列块和当前 pending 按时间顺序拼成一段。
        多媒体仍由 collect_pending_images 单独从当前 pending 收集。
        """
        entries = list(prefix_entries or [])
        entries.extend(self.pending_group_msgs)
        return self.render_messages(entries)

    def consume_pending_into_user(self):
        """清空 pending（user_content 已由调用方通过 build_user_content 预构建）。

        B2 方案：build_user_content 在 LLM 调用前执行（渲染文本），
        consume 在 LLM 调用后执行（只清空 pending，不重新 build）。
        """
        self.pending_group_msgs = []

    # ---------- 落地一轮对话 ----------
    def append_turn(self, user_content: str, assistant_content: str, save: bool = True):
        """追加一轮 user/assistant 对话。

        只负责追加与持久化；是否裁剪由 main.py 在记忆整理成功后调用 trim_to_recent。

        Args:
            user_content: 本轮群消息批次
            assistant_content: LLM 返回的行指令文本（内部记忆指令已剥离）
            save: 是否立即落盘。P006 成功路径会传 False，最后统一 save() 一次，
                  保证 queue/pending/messages 原子写入
        """
        self.messages.append({"role": "user", "content": user_content})
        self.messages.append({"role": "assistant", "content": assistant_content})
        if save:
            self._save()

    def turn_count(self) -> int:
        """当前 user/assistant 对数。"""
        return len(self.messages) // 2

    def trim_to_recent(self, keep_turns: int) -> int:
        """裁剪早期原文，只保留最近 keep_turns 轮（按整对裁剪）。

        Returns:
            被裁掉的轮数
        """
        keep_turns = max(1, int(keep_turns))
        keep_msgs = keep_turns * 2
        if len(self.messages) <= keep_msgs:
            return 0
        removed_turns = (len(self.messages) - keep_msgs) // 2
        self.messages = self.messages[-keep_msgs:]
        self._save()
        logger.info(f"记忆整理完成：裁剪 {removed_turns} 轮早期原文，保留最近 {keep_turns} 轮")
        return removed_turns

    # ---------- 查询 ----------
    def get_messages_for_llm(self) -> list[dict]:
        """返回传给 LLM 的 messages 列表（不含 system，system 由调用方拼接）。

        P003 方案 A：对**所有**历史 user message 做标记转换，消除"回复触发点"：
        - user content 开头的 "# 当前上下文" → "# 历史上下文"
        - 删除末尾的 "请按行指令输出。"（兼容旧历史的 "请输出 JSON。"）
        当前请求的 user content 由调用方单独作为最后一条 user 追加，不在 messages 里；
        因此历史里的每一条 user 都应按"历史上下文"处理。
        转换在返回时做，不持久化到 conversation.json，不影响其他功能。
        """
        result = []
        for m in self.messages:
            if m["role"] == "user":
                content = m["content"]
                # 替换开头标记
                if content.startswith("# 当前上下文"):
                    content = "# 历史上下文" + content[len("# 当前上下文"):]
                # 删除末尾的输出指令（兼容新协议与旧 JSON 历史）
                for suffix in ("\n\n请按行指令输出。", "\n请按行指令输出。", "请按行指令输出。",
                               "\n\n请输出 JSON。", "\n请输出 JSON。", "请输出 JSON。"):
                    if content.endswith(suffix):
                        content = content[:-len(suffix)]
                        break
                result.append({"role": "user", "content": content})
            else:
                result.append(m)
        return result

    def get_recent_speakers(self, recent_turns: int = 6) -> set:
        """获取最近 N 轮对话中发言过的 QQ 集合（含当前 pending）。

        扫描 messages 末尾 N 轮（每轮 = user + assistant）：
        - user content：只扫描 "# 最近群消息" 标记之后的消息行，
          用正则提取 nickname(qq) 模式中的 qq。跳过顶部群成员列表，
          否则 member_list 里的人会被误捕为"近期发言者"导致只增不减。
        - assistant content：扫描 `targets:` 行

        以及当前 pending_group_msgs 中的 qq。

        用于 _build_member_list 过滤：只展示近期活跃 + 有亲密度记录的成员，
        避免 user content 顶部塞入全群名单。

        Args:
            recent_turns: 扫描最近几轮（user/assistant 对），默认 6

        Returns:
            QQ 字符串集合
        """
        import re
        qqs = set()

        # 1. 当前 pending 中的发言者（本轮一定会被 LLM 看到）
        for entry in self.pending_group_msgs:
            qq = entry.get("qq")
            if qq:
                qqs.add(qq)

        # 2. 扫描 messages 末尾 N 轮
        recent_messages = self.messages[-(recent_turns * 2):] if recent_turns > 0 else []
        for msg in recent_messages:
            if msg["role"] == "user":
                # user content 顶部有群成员列表（也是 nickname(qq) 格式），
                # 必须按 "# 最近群消息" 分割，只扫描消息部分，否则会闭环累积
                content = msg["content"]
                marker = "# 最近群消息"
                idx = content.find(marker)
                if idx >= 0:
                    content = content[idx:]
                # user content 每行形如 [#msg_id] [time] nickname(qq): content
                # 用正则提取括号里的 QQ（5 位以上数字，过滤短数字干扰）
                for match in re.finditer(r'\((\d{5,})\)', content):
                    qqs.add(match.group(1))
            elif msg["role"] == "assistant":
                # assistant content 是行指令输出，targets 在 `targets:` 行
                for line in msg["content"].splitlines():
                    line = line.strip()
                    if line.startswith("targets:") or line.startswith("targets："):
                        payload = line[len("targets") + 1:]
                        for t in payload.split(","):
                            if t.strip():
                                qqs.add(t.strip())
                        break

        return qqs

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
        # 审计修复：存到 delayed 后必须清空 pending，否则下一轮会立刻重复处理同一批消息，
        # 且到期时 delayed 又把它们加回 pending，导致重复块/重复回复。
        self.pending_group_msgs = []
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
