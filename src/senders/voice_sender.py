"""语音发送器实现。

三通道：ai_record（NapCat AI 语音）/ local_file（本地音频转 OB11 record 段）/ url（网络音频转 record 段）。
通过 VoiceSender 接口隔离，未来可插入新 TTS 引擎。

设计说明：
- ai_record 通道：调用 NapCat 内置 /send_group_ai_record，零依赖，character 由 config 配置。
- local_file 通道：发送本地音频文件（OB11 record 段，data.file 字段）。作为"可复用原语"保留——
  未来接入 TTS 引擎时，TTS 输出重定向到文件后即可复用此 sender 发送 record 段，无需改动主流程。
- url 通道：发送网络音频（OB11 record 段，data.url 字段）。配合素材库 manifest 中的 type=url 条目使用。
"""
from pathlib import Path

from ..napcat_client import NapCatClient
from ..utils.logger import get_logger

logger = get_logger("voice_sender")


class AIRecordVoiceSender:
    """AI 语音发送器，调用 /send_group_ai_record。

    调用失败时（character 无效、网络错误等）按 fallback_to_text 决定是否降级为文字。
    """

    def __init__(self, client: NapCatClient, character: str, fallback_to_text: bool = True):
        self.client = client
        self.character = character
        self.fallback_to_text = fallback_to_text

    def send(self, group_id: str, voice_data: dict) -> dict:
        """发送 AI 语音。

        Args:
            group_id: 目标群号
            voice_data: 含 text 字段

        Returns:
            NapCat 返回的完整响应 dict；失败且降级时返回 text 段的响应；彻底失败返回 {}
        """
        text = voice_data.get("text", "")
        if not text:
            logger.warning("voice 段缺少 text，跳过")
            return {}

        # send_group_ai_record 内部已通过 _call 检查业务 status，失败返回 {}
        result = self.client.send_group_ai_record(group_id, self.character, text)
        if result:
            logger.info(f"AI 语音发送成功: {text[:30]}...")
            return result

        # 发送失败
        logger.error(f"AI 语音发送失败: character={self.character} text={text[:30]}...")
        if self.fallback_to_text:
            logger.info("降级为 text 段发送（fallback_to_text=true）")
            return self.client.send_group_msg(group_id, [
                {"type": "text", "data": {"text": text}},
            ])
        return {}


class LocalFileVoiceSender:
    """本地音频文件发送器，构造 OB11MessageRecord 段。

    作为可复用原语保留：未来接入 TTS 引擎时，TTS 输出重定向到文件后，
    可直接复用此 sender 发送 record 段，无需改动主流程。
    """

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

        # 转成 file:// URI：NapCat 是独立进程，CWD 与本进程不同，相对路径无法解析
        file_uri = Path(file_path).resolve().as_uri()
        record_seg = [{"type": "record", "data": {"file": file_uri}}]
        return self.client.send_group_msg(group_id, record_seg)


class UrlVoiceSender:
    """网络音频发送器，构造 OB11MessageRecord 段（data.url 字段）。

    配合素材库 manifest 中 type=url 的音频条目使用：LLM 从 system prompt 的素材库描述
    选取合适的 URL，原样输出到 voice 段 data.url，本 sender 透传给 NapCat。
    """

    def __init__(self, client: NapCatClient):
        self.client = client

    def send(self, group_id: str, voice_data: dict) -> dict:
        """发送网络音频。

        Args:
            group_id: 目标群号
            voice_data: 含 url 字段（网络音频地址）
        """
        url = voice_data.get("url", "")
        if not url:
            logger.warning("voice url 段缺少 url，跳过")
            return {}

        record_seg = [{"type": "record", "data": {"url": url}}]
        return self.client.send_group_msg(group_id, record_seg)
