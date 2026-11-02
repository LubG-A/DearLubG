"""聊天上下文基类（群聊 / 私聊共用）。

只保留场景无关的运行时状态：
- history（messages / summary / pending / fast_buffer / delayed_replies）
- last_reply_time（冷却计时）
- 触发定时器（群聊静默窗口；私聊不使用）
- _cycle_pending（撞 cycle 重试标志）
- 补触发定时器（P003：Bot 发言期间到达但未被 LLM 看到的消息）

场景差异由子类补充：
- GroupContext：评分、归因、direct 状态、主动触发调度器
- PrivateContext：对方 QQ / 昵称（无评分/归因/主动触发）
"""
import threading
from typing import Optional

from .config import Config
from .history import HistoryManager
from .llm_client import LLMClient


class ChatContext:
    """聊天上下文数据容器基类。"""

    def __init__(self, chat_type: str, chat_id: str, config: Config,
                 llm: LLMClient, state_dir: str):
        self.chat_type = chat_type          # "group" / "private"
        self.chat_id = str(chat_id)         # group_id / 对方 QQ
        self.config = config

        self.history = HistoryManager(config.trigger, state_dir=state_dir)
        self.history.set_summarizer(llm.summarize)

        # 冷却计时：最近一次实际发出回复的时间
        self.last_reply_time: float = 0.0

        # 静默窗口定时器（群聊使用；私聊每条消息直接触发，不使用）
        self._quiet_timer: Optional[threading.Timer] = None
        self._quiet_timer_lock = threading.Lock()

        # cycle 串行重试标志（per-chat）
        self._cycle_pending: bool = False

        # P003 补触发定时器（群聊与私聊共用）
        self._buffer_followup_timer: Optional[threading.Timer] = None
        self._buffer_followup_lock = threading.Lock()

    @property
    def chat_key(self) -> str:
        """全局唯一 chat key，用于 cycle 队列。"""
        return f"{self.chat_type}:{self.chat_id}"
