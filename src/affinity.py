"""亲密度管理模块。

存储 {qq: affinity}，由 LLM 输出 affinity_delta 微调。
初值统一为 0，避免预设差异化初值。
"""
import json
from pathlib import Path

from .utils.logger import get_logger

logger = get_logger("affinity")

AFFINITY_MIN = 0
AFFINITY_MAX = 100
DELTA_MIN = -2
DELTA_MAX = 2


class AffinityManager:
    """亲密度管理器。"""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(exist_ok=True)
        self.file = self.state_dir / "affinity.json"
        # {qq: {"value": int, "last_updated": str}}
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.file.exists():
            try:
                self.data = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"加载亲密度失败: {e}")

    def _save(self):
        self.file.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, qq: str) -> int:
        """获取亲密度，缺失返回 0。"""
        return self.data.get(qq, {}).get("value", 0)

    def apply_delta(self, delta_map: dict):
        """应用 LLM 输出的 affinity_delta。

        Args:
            delta_map: {qq: delta}，delta 会 clip 到 [-2, +2]
        """
        for qq, delta in delta_map.items():
            # clip delta
            delta = max(DELTA_MIN, min(DELTA_MAX, delta))
            old = self.get(qq)
            new = max(AFFINITY_MIN, min(AFFINITY_MAX, old + delta))
            self.data[qq] = {
                "value": new,
                "last_updated": __import__("datetime").datetime.now().isoformat(),
            }
            logger.debug(f"亲密度 {qq}: {old} -> {new} (delta={delta})")
        self._save()
