"""emoji 反应器。

调用 NapCat /set_msg_emoji_like 给群消息添加 emoji 反应。
主流程通过 EmojiReactor 接口依赖，空实现 EmptyEmojiReactor 作为兜底（napcat_client 缺失时用）。
"""
from typing import Optional

from ..napcat_client import NapCatClient
from ..utils.logger import get_logger

logger = get_logger("emoji_reactor")


class EmojiReactor:
    """emoji 反应器：调用 /set_msg_emoji_like 给消息加 emoji。"""

    def __init__(self, napcat: NapCatClient):
        self.napcat = napcat

    def react(self, group_id: str, msg_id: str, emoji_id: str) -> dict:
        """给指定消息添加 emoji 反应。

        Args:
            group_id: 群号（当前 NapCat 接口不直接用 group_id，message_id 已定位消息，
                      保留参数与 EmptyEmojiReactor 签名一致，便于替换）
            msg_id: 目标消息 ID（由 history.get_msg_id_by_id 校验后传入）
            emoji_id: emoji ID（字符串）

        Returns:
            NapCat 返回的 dict；失败（msg_id 空、NapCat 调用失败）返回空 dict。
        """
        if not msg_id:
            logger.warning(f"emoji 反应跳过：msg_id 为空（emoji_id={emoji_id}）")
            return {}
        if not emoji_id:
            logger.warning(f"emoji 反应跳过：emoji_id 为空（msg_id={msg_id}）")
            return {}
        logger.info(f"添加 emoji 反应: msg_id={msg_id} emoji_id={emoji_id}")
        resp = self.napcat.set_msg_emoji_like(msg_id, emoji_id, set_=True)
        if not resp:
            logger.warning(f"emoji 反应失败: msg_id={msg_id} emoji_id={emoji_id}")
            return {}
        return resp


class EmptyEmojiReactor:
    """emoji 反应器预留实现（napcat_client 未注入时用）。"""

    def react(self, group_id: str, msg_id: str, emoji_id: str) -> dict:
        """预留实现：仅记日志，不发送。"""
        logger.info(f"[预留] emoji 反应被跳过 msg_id={msg_id} emoji_id={emoji_id}")
        return {}
