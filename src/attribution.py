"""归因自校准模块。

根据 LLM 返回结果调整软因子的 dynamic_weight。
包含冷启动保护。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config
from .trigger import TriggerFactor
from .utils.logger import get_logger

logger = get_logger("attribution")


class AttributionManager:
    """归因管理器。"""

    def __init__(self, config: Config, state_dir: str = "state"):
        self.config = config
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.log_file = self.state_dir / "attribution_log.jsonl"
        # dynamic_weights: {factor_name: float}
        self.dynamic_weights: dict[str, float] = {}
        self.round_count: int = 0
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.dynamic_weights = data.get("dynamic_weights", {})
                self.round_count = data.get("round_count", 0)
            except Exception as e:
                logger.warning(f"加载归因状态失败: {e}")

    def _save(self):
        self.state_file.write_text(
            json.dumps({
                "dynamic_weights": self.dynamic_weights,
                "round_count": self.round_count,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_dynamic_weight(self, factor_name: str, base_value: int) -> float:
        """获取因子的 dynamic_weight，缺失则初始化为 0。"""
        if factor_name not in self.dynamic_weights:
            self.dynamic_weights[factor_name] = 0.0
        return self.dynamic_weights[factor_name]

    # ---------- 归因更新 ----------
    def update(self, soft_factors: list[TriggerFactor], llm_action: str):
        """根据 LLM 返回结果更新软因子权重。

        Args:
            soft_factors: 本轮参与触发的软因子列表
            llm_action: LLM 返回的 action（silent/reply/multi_reply/react）
        """
        self.round_count += 1
        step = self.config.trigger.attribution_step
        is_silent = llm_action == "silent"

        # 写归因日志（冷启动期与正常期都写）
        log_entry = {
            "round": self.round_count,
            "timestamp": datetime.now().isoformat(),
            "action": llm_action,
            "factors": [
                {
                    "name": f.name,
                    "base_value": f.base_value,
                    "dynamic_weight_before": self.get_dynamic_weight(f.name, f.base_value),
                    "effective_value": f.effective_value,
                }
                for f in soft_factors
            ],
        }

        # 冷启动保护：前 N 轮仅记日志，不更新
        if self.round_count <= self.config.trigger.cold_start_rounds:
            log_entry["updated"] = False
            log_entry["reason"] = "cold_start"
            self._write_log(log_entry)
            logger.debug(f"冷启动期 round={self.round_count}，仅记日志")
            self._save()
            return

        # 正常更新
        updates = []
        for f in soft_factors:
            v = f.effective_value
            old_dw = self.get_dynamic_weight(f.name, f.base_value)
            if is_silent:
                new_dw = old_dw - v * step
            else:
                new_dw = old_dw + v * step * 0.5
            # clip: [-base_value, +base_value]
            new_dw = max(-f.base_value, min(f.base_value, new_dw))
            self.dynamic_weights[f.name] = new_dw
            updates.append({
                "name": f.name,
                "dynamic_weight_before": old_dw,
                "dynamic_weight_after": new_dw,
            })

        log_entry["updated"] = True
        log_entry["updates"] = updates
        self._write_log(log_entry)
        self._save()

    def _write_log(self, entry: dict):
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
