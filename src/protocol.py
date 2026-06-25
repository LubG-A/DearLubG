"""协议接口定义。

主流程只依赖以下 4 个协议接口，不依赖具体实现。
各接口的"空实现"或"预留实现"在后续迭代中替换为真实实现时，主流程零修改。
"""
from typing import Protocol, Any


class MessageSender(Protocol):
    """统一消息发送入口，负责把 OneBot 消息段数组发到指定群。"""

    def send_group_message(self, group_id: str, segments: list[dict]) -> dict:
        """发送消息段数组到指定群。

        Args:
            group_id: 目标群号
            segments: OneBot 消息段列表，如 [{"type":"text","data":{"text":"..."}}]

        Returns:
            NapCat 返回的响应字典，包含 message_id 等
        """
        ...


class VoiceSender(Protocol):
    """语音发送接口。channel 字段决定具体实现（ai_record / local_file）。"""

    def send(self, group_id: str, voice_data: dict) -> dict:
        """发送语音消息。

        Args:
            group_id: 目标群号
            voice_data: 语音数据，含 channel/text/file 等字段

        Returns:
            NapCat 返回的响应字典
        """
        ...


class ImageSender(Protocol):
    """图片发送接口。当前阶段为空实现。"""

    def send(self, group_id: str, image_data: dict) -> dict:
        """发送图片消息。

        Args:
            group_id: 目标群号
            image_data: 图片数据，含 url/summary 等字段

        Returns:
            NapCat 返回的响应字典
        """
        ...


class EmojiReactor(Protocol):
    """emoji 反应接口。当前阶段为预留实现，等 image 系统完成后统一实现。"""

    def react(self, group_id: str, msg_id: str, emoji_id: str) -> dict:
        """对指定消息做 emoji 反应。

        Args:
            group_id: 目标群号
            msg_id: 被反应的消息 ID
            emoji_id: OneBot emoji ID

        Returns:
            NapCat 返回的响应字典
        """
        ...
