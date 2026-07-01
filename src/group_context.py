"""群上下文容器（per-group state container）。

持有单个群的所有独立状态：
- conversation history（messages, pending, fast_buffer, delayed_replies）
- attribution（dynamic_weight 按群独立）
- trigger_evaluator（使用本群 history + 全局 affinity）
- active_scheduler（本群主动触发调度器，阶段 D）
- last_reply_time（本群冷却计时）
- quiet_timer（本群静默窗口）
- cycle_pending（本群撞 cycle 重试标志）

全局共享单例（napcat, llm, affinity, persona）由 Bot 注入引用。

阶段 A：纯结构抽取，单群运行，零行为变化。
阶段 B：per-group 子目录 state/{group_id}/，多群独立文件存储。
阶段 D：主动触发调度器移入本类，每群独立倒计时。
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
from .scheduler import ActiveScheduler


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
        state_dir: str = "state",
    ):
        self.group_id = group_id
        self.config = config

        # 共享单例引用（不拷贝）
        self.napcat = napcat
        self.llm = llm
        self.affinity = affinity

        # per-group 状态管理器（子目录隔离：state/{group_id}/）
        per_group_state_dir = f"{state_dir}/{group_id}"
        self.history = HistoryManager(config.trigger, state_dir=per_group_state_dir)
        self.history.set_summarizer(llm.summarize)
        self.attribution = AttributionManager(config, state_dir=per_group_state_dir)
        self.trigger_evaluator = TriggerEvaluator(
            config, self.history, self_qq, persona_name,
            affinity_manager=affinity,
        )
        # 阶段 D：per-group 主动触发调度器（从全局单例移入）
        self.active_scheduler = ActiveScheduler(
            config, group_id=group_id, state_dir=per_group_state_dir,
        )

        # per-group 运行时状态
        self.last_reply_time: float = 0.0
        self._quiet_timer: Optional[threading.Timer] = None
        self._quiet_timer_lock = threading.Lock()
        self._cycle_pending: bool = False
