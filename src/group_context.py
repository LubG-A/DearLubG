"""群上下文容器（per-group state container）。

在 ChatContext 基础上补充群聊专属状态：
- attribution（dynamic_weight 按群独立）
- trigger_evaluator（使用本群 history + 全局 affinity）
- active_scheduler（本群主动触发调度器，阶段 D）
- conversation_mode / direct_targets（阶段 F direct 快速触发）

全局共享单例（napcat, llm, affinity, persona）由 Bot 注入引用。

阶段 A：纯结构抽取，单群运行，零行为变化。
阶段 B：per-group 子目录 state/{group_id}/，多群独立文件存储。
阶段 D：主动触发调度器移入本类，每群独立倒计时。
阶段 I：共享状态上提到 ChatContext 基类，私聊复用同一套 cycle。
"""
from .chat_context import ChatContext
from .config import Config
from .attribution import AttributionManager
from .trigger import TriggerEvaluator
from .affinity import AffinityManager
from .napcat_client import NapCatClient
from .llm_client import LLMClient
from .scheduler import ActiveScheduler


class GroupContext(ChatContext):
    """单个群的上下文容器。

    设计取向（决策 A1）：本类作为"数据容器"，方法留在 Bot 并接受 ctx 参数。
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
        per_group_state_dir = f"{state_dir}/{group_id}"
        super().__init__("group", group_id, config, llm, state_dir=per_group_state_dir)

        self.group_id = group_id

        # 共享单例引用（不拷贝）
        self.napcat = napcat
        self.llm = llm
        self.affinity = affinity

        # per-group 状态管理器（子目录隔离：state/{group_id}/）
        self.attribution = AttributionManager(config, state_dir=per_group_state_dir)
        self.trigger_evaluator = TriggerEvaluator(
            config, self.history, self_qq, persona_name,
            affinity_manager=affinity,
        )
        # 阶段 D：per-group 主动触发调度器（从全局单例移入）
        self.active_scheduler = ActiveScheduler(
            config, group_id=group_id, state_dir=per_group_state_dir,
        )

        # 阶段 F：对话活跃度感知（direct 模式快速触发）
        # conversation_mode: "direct"=Bot 正在与某人往返对话 / "open"=开放插话
        # direct_targets: {qq: deadline_timestamp}，每个 target 各自独立窗口（DIRECT_WINDOW_SECONDS，默认 90s）
        self.conversation_mode: str = "open"
        self.direct_targets: dict[str, float] = {}
