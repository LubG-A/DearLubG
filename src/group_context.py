"""群上下文容器（per-group state container）。

持有单个群的所有独立状态：
- conversation history（messages, pending, fast_buffer, delayed_replies）
- attribution（dynamic_weight 按群独立）
- trigger_evaluator（使用本群 history + 全局 affinity）
- last_reply_time（本群冷却计时）
- quiet_timer（本群静默窗口）
- cycle_pending（本群撞 cycle 重试标志）

全局共享单例（napcat, llm, affinity, persona）由 Bot 注入引用。

阶段 A：纯结构抽取，单群运行，零行为变化。
阶段 B 起才真正多群化（文件名加 group_id 后缀、napcat 方法接受 group_id 等）。
"""
import threading
from typing import Optional

from .config import Config
from .history import HistoryManager
from .attribution import AttributionManager
from .trigger import TriggerEvaluator
from .affinity import AffinityManager
from .napcat_client import NapCatClient
from .llm_client import LLMClient


class GroupContext:
    """单个群的上下文容器。

    设计取向（决策 A1）：本类作为"数据容器"，方法留在 Bot 并接受 ctx 参数。
    后续阶段可逐步把方法从 Bot 移入本类，每次移动一个方法并独立验证。
    """

    def __init__(
        self,
        group_id: str,
        config: Config,
        napcat: NapCatClient,
        llm: LLMClient,
        affinity: AffinityManager,
        self_qq: str,
        persona_name: str,
    ):
        self.group_id = group_id
        self.config = config

        # 共享单例引用（不拷贝）
        self.napcat = napcat
        self.llm = llm
        self.affinity = affinity

        # per-group 状态管理器
        # 注：阶段 A 保持默认文件名（conversation.json / state.json），阶段 B 再参数化
        self.history = HistoryManager(config.trigger)
        self.history.set_summarizer(llm.summarize)
        self.attribution = AttributionManager(config)
        self.trigger_evaluator = TriggerEvaluator(
            config, self.history, self_qq, persona_name,
            affinity_manager=affinity,
        )

        # per-group 运行时状态
        self.last_reply_time: float = 0.0
        self._quiet_timer: Optional[threading.Timer] = None
        self._quiet_timer_lock = threading.Lock()
        self._cycle_pending: bool = False
