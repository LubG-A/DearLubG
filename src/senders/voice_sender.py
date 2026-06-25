"""语音发送器实现。

双通道：ai_record（NapCat AI 语音）/ local_file（本地音频转 OB11MessageRecord）。
通过 VoiceSender 接口隔离，未来可插入新 TTS 引擎。
"""
from pathlib import Path

from ..napcat_client import NapCatClient
from ..utils.logger import get_logger

logger = get_logger("voice_sender")


class AIRecordVoiceSender:
    """AI 语音发送器，调用 /send_group_ai_record。"""

    def __init__(self, client: NapCatClient, character: str, fallback_to_text: bool = False):
        self.client = client
        self.character = character
        self.fallback_to_text = fallback_to_text

    def send(self, group_id: str, voice_data: dict) -> dict:
        """发送 AI 语音。

        Args:
            group_id: 目标群号
            voice_data: 含 text 字段
        """
        text = voice_data.get("text", "")
        if not text:
            logger.warning("voice 段缺少 text，跳过")
            return {}

        try:
            return self.client.send_group_ai_record(self.character, text)
        except Exception as e:
            logger.error(f"AI 语音发送失败: {e}")
            if self.fallback_to_text:
                logger.info("降级为 text 段发送")
                return self.client.send_group_msg([
                    {"type": "text", "data": {"text": text}},
                ])
            return {}


class LocalFileVoiceSender:
    """本地音频文件发送器，构造 OB11MessageRecord 段。"""

    def __init__(self, client: NapCatClient):
        self.client = client

    def send(self, group_id: str, voice_data: dict) -> dict:
        """发送本地音频文件。

        Args:
            group_id: 目标群号
            voice_data: 含 file 字段（本地音频路径）
        """
        file_path = voice_data.get("file", "")
        if not file_path or not Path(file_path).exists():
            logger.warning(f"voice 本地文件不存在: {file_path}")
            return {}

        # 构造 OB11MessageRecord 段（file:// 协议或绝对路径）
        record_seg = [{"type": "record", "data": {"file": file_path}}]
        return self.client.send_group_msg(record_seg)
