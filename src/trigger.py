"""触发评分机制（第一层：看一眼阈值）。

只做粗筛，决定"要不要让 LLM 看一眼"，不决定要不要回复。
"""
from datetime import datetime
from typing import Optional

from .config import Config
from .history import HistoryManager
from .utils.logger import get_logger

logger = get_logger("trigger")

# friend_speak 线性插值参数
FRIEND_SPEAK_HIT_THRESHOLD = 5    # 亲密度低于此值视为陌生人，不加分
FRIEND_SPEAK_MAX_BONUS = 25       # 亲密度=100 时的最大加分
FRIEND_SPEAK_SCALE = FRIEND_SPEAK_MAX_BONUS / 100  # = 0.25


class TriggerFactor:
    """单个触发因子的计算结果。"""

    def __init__(self, name: str, base_value: int, is_hard: bool, dynamic_weight: float = 0.0, hit: bool = False):
        self.name = name
        self.base_value = base_value
        self.is_hard = is_hard            # 硬因子不参与归因
        self.dynamic_weight = dynamic_weight
        self.hit = hit                    # 本轮是否命中（用于决定是否计入总分）

    @property
    def effective_value(self) -> float:
        """有效贡献 = base_value + dynamic_weight。"""
        return self.base_value + self.dynamic_weight


class TriggerEvaluator:
    """触发评分评估器。"""

    def __init__(self, config: Config, history: HistoryManager, self_qq: str, persona_name: str,
                 affinity_manager=None):
        self.config = config
        self.history = history
        self.self_qq = str(self_qq)
        self.persona_name = persona_name
        self.affinity_manager = affinity_manager  # 可选，阶段二接入

    def evaluate(self, msg: dict) -> tuple[float, list[TriggerFactor]]:
        """评估单条消息，返回 (有效总分, 参与触发的软因子列表)。"""
        factors: list[TriggerFactor] = []
        text = msg.get("raw_message", "") or _extract_text(msg.get("message", []))
        sender_qq = str(msg.get("user_id", ""))

        # 硬因子：被 @ 或被昵称直呼
        at_me = self._check_at_me(msg) or self.persona_name in text
        factors.append(TriggerFactor("at_me", self.config.trigger_hard_factors["at_me"], is_hard=True))

        # 硬因子：针对自己的提问语气
        question_to_me = self._check_question_to_me(text)
        factors.append(TriggerFactor("question_to_me", self.config.trigger_hard_factors["question_to_me"], is_hard=True))

        # 软因子：兴趣关键词
        interest_hit = any(kw in text for kw in self.config.persona.interests)
        factors.append(TriggerFactor(
            "interest_keyword", self.config.trigger_factors["interest_keyword"],
            is_hard=False,
        ))

        # 软因子：熟人发言（亲密度线性插值）
        # 亲密度 ≥5 才命中；贡献 = affinity × 0.25（亲密度=100 时加 25 分）
        # 实现：base_value 设为亲密度贡献值（动态），dynamic_weight 仍由归因调整
        #   effective_value = base_value + dynamic_weight = (affinity × 0.25) + dynamic_weight
        # 归因 clip 时需要用 max(base_value, FRIEND_SPEAK_MAX_BONUS) 作为上界
        affinity_value = 0
        friend_speak_hit = False
        if self.affinity_manager is not None:
            affinity_value = self.affinity_manager.get(sender_qq)
            if affinity_value >= FRIEND_SPEAK_HIT_THRESHOLD:
                friend_speak_hit = True
        if friend_speak_hit:
            friend_speak_base = int(affinity_value * FRIEND_SPEAK_SCALE)  # 亲密度贡献作为 base_value
        else:
            friend_speak_base = 0
        factors.append(TriggerFactor(
            "friend_speak", friend_speak_base,
            is_hard=False,
            hit=friend_speak_hit,
        ))

        # 软因子：话题热度
        topic_hot = self.history.recent_message_count(180) >= 5
        factors.append(TriggerFactor(
            "topic_hot", self.config.trigger_factors["topic_hot"],
            is_hard=False,
        ))

        # 硬因子：深夜时段
        hour = datetime.now().hour
        late_night = 23 <= hour or hour < 5
        factors.append(TriggerFactor(
            "late_night", self.config.trigger_hard_factors["late_night"], is_hard=True,
        ))

        # 硬因子：刷屏/纯表情/转发链
        spam = self._check_spam(text)
        factors.append(TriggerFactor("spam", self.config.trigger_hard_factors["spam"], is_hard=True))

        # 计算有效总分
        total = 0.0
        for f in factors:
            if f.name == "at_me" and at_me:
                total += f.effective_value
            elif f.name == "question_to_me" and question_to_me:
                total += f.effective_value
            elif f.name == "interest_keyword" and interest_hit:
                total += f.effective_value
            elif f.name == "friend_speak" and f.hit:
                total += f.effective_value
            elif f.name == "topic_hot" and topic_hot:
                total += f.effective_value
            elif f.name == "late_night" and late_night:
                total += f.effective_value
            elif f.name == "spam" and spam:
                total += f.effective_value

        soft_factors = [f for f in factors if not f.is_hard and f.effective_value != 0]
        return total, soft_factors

    def should_peek(self, score: float) -> bool:
        """是否达到"看一眼"阈值。"""
        return score >= self.config.trigger.peek_threshold

    # ---------- 判断函数 ----------
    def _check_at_me(self, msg: dict) -> bool:
        for seg in msg.get("message", []):
            if seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == self.self_qq:
                return True
        return False

    def _check_question_to_me(self, text: str) -> bool:
        markers = ["?", "？", "吗", "么", "呢", "说", "看"]
        return self.persona_name in text and any(m in text for m in markers)

    def _check_spam(self, text: str) -> bool:
        # 纯表情或过短内容视为刷屏
        return len(text.strip()) <= 1 and text.strip() != ""


def _extract_text(segments: list) -> str:
    """从消息段数组提取纯文本。"""
    parts = []
    for seg in segments:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)
