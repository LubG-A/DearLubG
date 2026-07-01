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
        return self.client.send_group_msg(group_id, segments)

    def build_segments(self, messages: list, history) -> list[list[dict]]:
        """把模型输出的 messages 转换为 OneBot 消息段数组的列表。

        每条消息转换为一段数组（可能含多段），返回多条消息的段数组列表。

        Args:
            messages: 模型输出的 messages 列表，元素支持三种形式：
                      - str：纯文本简写（单 text 段）
                      - dict：单段消息简写
                      - list：多段混合消息（一条消息含多段，如 text+face）
            history: 历史记录管理器（用于 reply 段校验 target_msg_id）

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
        """转换单条消息为段数组。

        支持三种输入：
        - str：等价于 [{"type":"text","data":{"text":msg}}]
        - dict：单段消息，按 type 分发
        - list：多段混合消息，逐段转换后拼接（保持顺序）
        """
        if isinstance(msg, str):
            return [{"type": "text", "data": {"text": msg}}]

        if isinstance(msg, list):
            # 多段混合消息：逐段转换并拼接
            segs = []
            for seg in msg:
                built = self._build_one(seg, history)
                segs.extend(built)
            return segs

        if not isinstance(msg, dict):
            return []

        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        if msg_type == "text":
            return [{"type": "text", "data": {"text": data.get("text", "")}}]

        if msg_type == "at":
            return [{"type": "at", "data": {"qq": str(data.get("qq", "")), "text": data.get("text", "")}}]

        if msg_type == "reply":
            target_msg_id = data.get("target_msg_id", "")
            msg_id = history.get_msg_id_by_id(target_msg_id) if history else None
            segs = []
            if msg_id:
                segs.append({"type": "reply", "data": {"id": msg_id}})
            else:
                logger.warning(f"reply 段 target_msg_id={target_msg_id} 无效，跳过 reply 段（保留附文）")
            if data.get("text"):
                segs.append({"type": "text", "data": {"text": data["text"]}})
            return segs

        if msg_type == "face":
            return [{"type": "face", "data": {"id": str(data.get("id", ""))}}]

        if msg_type == "poke":
            # 戳一戳：data.qq 指定被戳者，透传给 NapCat
            return [{"type": "poke", "data": {"qq": str(data.get("qq", ""))}}]

        if msg_type == "forward":
            # forward 由专用 API 发送，这里返回标记
            return [{"type": "forward", "data": data}]

        if msg_type == "voice":
            # voice 由 main._send_messages 根据 channel 分发到 AIRecordVoiceSender / LocalFileVoiceSender
            return [{"type": "voice", "data": data}]

        logger.warning(f"未知消息段 type={msg_type}，跳过")
        return []
