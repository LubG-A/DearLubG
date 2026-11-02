"""私聊上下文容器（per-user state container）。

私聊是群聊的减法：一对一对话不需要评分、归因、direct 状态、主动触发调度器。
本类只保留 ChatContext 的通用状态 + 对方身份信息。

状态隔离：每个私聊对象一个子目录 state/private/{qq}/conversation.json。
"""
import threading
from typing import Optional

from .chat_context import ChatContext
from .config import Config
from .llm_client import LLMClient


class PrivateContext(ChatContext):
    """单个私聊对象的上下文容器。"""

    def __init__(self, peer_qq: str, peer_nickname: str, config: Config,
                 llm: LLMClient, state_dir: str = "state"):
        peer_qq = str(peer_qq)
        super().__init__("private", peer_qq, config, llm,
                         state_dir=f"{state_dir}/private/{peer_qq}")
        self.peer_qq = peer_qq
        self.peer_nickname = peer_nickname or peer_qq
        # 延迟回复（later）到期唤醒定时器；群聊有主动触发兜底，私聊需要自己唤醒
        self._delayed_timer: Optional[threading.Timer] = None
        self._delayed_timer_lock = threading.Lock()
