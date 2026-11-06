"""聊天上下文基类（群聊 / 私聊共用）。

只保留场景无关的运行时状态：
- history（messages / pending / fast_buffer / delayed_replies）
- memory（长期记忆 core / 近期日记 journal / 短期备忘 memo）
- last_reply_time（冷却计时）
- 触发定时器（群聊静默窗口；私聊不使用）
- _cycle_pending（撞 cycle 重试标志）
- 同时发言标记 + direct 回复倒计时（P003 v2.4）
- 最近对话块队列（P006：由 HistoryManager 随 conversation.json 持久化；本类只保存最近 state）

场景差异由子类补充：
- GroupContext：评分、归因、direct 状态、主动触发调度器
- PrivateContext：对方 QQ / 昵称 + 主动触发调度器（无评分/归因）
"""
import threading
from typing import Optional

from .config import Config
from .history import HistoryManager
from .memory import MemoryManager


class ChatContext:
    """聊天上下文数据容器基类。"""

    def __init__(self, chat_type: str, chat_id: str, config: Config, state_dir: str):
        self.chat_type = chat_type          # "group" / "private"
        self.chat_id = str(chat_id)         # group_id / 对方 QQ
        self.config = config

        self.history = HistoryManager(config.trigger, state_dir=state_dir)
        # 记忆系统：长期记忆 / 近期日记 / 短期备忘（per-chat，独立于 messages）
        self.memory = MemoryManager(config, state_dir=state_dir)

        # 冷却计时：最近一次实际发出回复的时间
        self.last_reply_time: float = 0.0

        # 静默窗口定时器（群聊使用；私聊每条消息直接触发，不使用）
        self._quiet_timer: Optional[threading.Timer] = None
        self._quiet_timer_lock = threading.Lock()

        # cycle 串行重试标志（per-chat）
        self._cycle_pending: bool = False

        # 同时发言标记 + direct 回复倒计时（P003 v2.4）
        # _cycle_active：本 chat 的 LLM cycle 是否正在跑
        # _backlog_pending：Bot 输出期间有新消息进入（信号量/标记）
        # _countdown_timer/_lock/_gen/_resets：direct 回复倒计时（target 停口 N 秒后才回）
        self._cycle_active: bool = False
        self._backlog_pending: bool = False
        self._countdown_timer: Optional[threading.Timer] = None
        self._countdown_lock = threading.Lock()
        self._countdown_resets: int = 0
        self._countdown_gen: int = 0

        # 打断模式（P005）
        # _send_interrupt：发送窗口（含 LLM 调用期间）到达新内容时置位；LLM 线程在发送前检查
        # _interrupt_sender：触发打断的发言者昵称（用于下一轮提示）
        # _unsent_intent：被打断、尚未发出的计划，下一轮注入 user content
        # _interrupt_streak：连续"完整打断"次数，达到上限后强制发出一次，防止被饿死
        self._send_interrupt = threading.Event()
        self._interrupt_sender: str = ""
        self._unsent_intent: Optional[dict] = None
        self._interrupt_streak: int = 0

        # P006：最近对话块队列已移入 HistoryManager（随 conversation.json 持久化），
        # 这里只保存最近一次 LLM 输出的 state（用于注入"你上一轮的状态"）；
        # 启动时从持久化队列的最后一块恢复。
        self._last_state: dict = {}
        if self.history.recent_blocks:
            state = self.history.recent_blocks[-1].get("state")
            if isinstance(state, dict):
                self._last_state = state

    @property
    def chat_key(self) -> str:
        """全局唯一 chat key，用于 cycle 队列。"""
        return f"{self.chat_type}:{self.chat_id}"
