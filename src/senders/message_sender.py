"""消息发送器实现。

负责 text/at/reply/face/forward 等普通段的拼装与发送。
"""
from typing import Optional

from ..napcat_client import NapCatClient
from ..utils.logger import get_logger

logger = get_logger("message_sender")


class NapCatMessageSender:
    """NapCat 消息发送器，实现 MessageSender 协议。"""

    def __init__(self, client: NapCatClient):
        self.client = client

    def send_group_message(self, group_id: str, segments: list[dict]) -> dict:
        """发送消息段数组到指定群。"""
        return self.client.send_group_msg(segments)

    def build_segments(self, messages: list, history) -> list[list[dict]]:
        """把模型输出的 messages 转换为 OneBot 消息段数组的列表。

        每条消息转换为一段数组（可能含多段），返回多条消息的段数组列表。

        Args:
            messages: 模型输出的 messages 列表（字符串或 dict）
            history: 历史记录管理器（用于 reply 段取 message_id）

        Returns:
            list of list of segment dict
        """
        result = []
        for msg in messages:
            segs = self._build_one(msg, history)
            if segs:
                result.append(segs)
        return result

    def _build_one(self, msg, history) -> list[dict]:
        """转换单条消息为段数组。"""
        if isinstance(msg, str):
            return [{"type": "text", "data": {"text": msg}}]

        if not isinstance(msg, dict):
            return []

        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        if msg_type == "text":
            return [{"type": "text", "data": {"text": data.get("text", "")}}]

        if msg_type == "at":
            return [{"type": "at", "data": {"qq": str(data.get("qq", "")), "text": data.get("text", "")}}]

        if msg_type == "reply":
            target_idx = data.get("target_msg_index", 0)
            msg_id = history.get_msg_id_by_index(target_idx) if history else ""
            segs = []
            if msg_id:
                segs.append({"type": "reply", "data": {"id": msg_id}})
            if data.get("text"):
                segs.append({"type": "text", "data": {"text": data["text"]}})
            return segs

        if msg_type == "face":
            return [{"type": "face", "data": {"id": str(data.get("id", ""))}}]

        if msg_type == "forward":
            # forward 由专用 API 发送，这里返回标记
            return [{"type": "forward", "data": data}]

        logger.warning(f"未知消息段 type={msg_type}，跳过")
        return []
