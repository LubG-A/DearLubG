"""私聊上下文容器（per-user state container）。

私聊是群聊的减法：一对一对话不需要评分、归因、direct 状态。
本类保留 ChatContext 的通用状态 + 对方身份信息 + 私聊主动触发调度器。

状态隔离：每个私聊对象一个子目录 state/private/{qq}/（conversation.json + scheduler.json）。
"""
import threading
from typing import Optional

from .chat_context import ChatContext
from .config import ActiveTriggerConfig, Config
from .llm_client import LLMClient
from .scheduler import ActiveScheduler


class PrivateContext(ChatContext):
    """单个私聊对象的上下文容器。"""

    def __init__(self, peer_qq: str, peer_nickname: str, config: Config,
                 llm: LLMClient, state_dir: str = "state"):
        peer_qq = str(peer_qq)
        per_private_state_dir = f"{state_dir}/private/{peer_qq}"
        super().__init__("private", peer_qq, config, llm,
                         state_dir=per_private_state_dir)
        self.peer_qq = peer_qq
        self.peer_nickname = peer_nickname or peer_qq
        # 延迟回复（later）到期唤醒定时器
        self._delayed_timer: Optional[threading.Timer] = None
        self._delayed_timer_lock = threading.Lock()

        # 私聊主动触发调度器：配置缺省时关闭，避免未配置就主动打扰对方
        private_cfg = getattr(config, "private", None)
        active_cfg = getattr(private_cfg, "active_trigger", None) if private_cfg else None
        if active_cfg is None:
            active_cfg = ActiveTriggerConfig(enabled=False)
        self.active_scheduler = ActiveScheduler(
            config, group_id=peer_qq, state_dir=per_private_state_dir,
            active_config=active_cfg, label=f"私聊 {peer_qq}",
        )
