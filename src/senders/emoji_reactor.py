"""emoji 反应器（预留实现）。

等 image 系统完成后统一实现。
"""
from ..utils.logger import get_logger

logger = get_logger("emoji_reactor")


class EmptyEmojiReactor:
    """emoji 反应器预留实现。"""

    def react(self, group_id: str, msg_id: str, emoji_id: str) -> dict:
        """预留实现：仅记日志，不发送。"""
        logger.info(f"[预留] emoji 反应被跳过 msg_id={msg_id} emoji_id={emoji_id}")
        return {}
