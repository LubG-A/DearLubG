"""语音发送器实现。

三通道：ai_record（NapCat AI 语音）/ local_file（本地音频转 OB11 record 段）/ url（网络音频下载后转 record 段）。
通过 VoiceSender 接口隔离，未来可插入新 TTS 引擎。

设计说明：
- ai_record 通道：调用 NapCat 内置 /send_group_ai_record，零依赖，character 由 config 配置。
- local_file 通道：发送本地音频文件（OB11 record 段，data.file 字段）。作为"可复用原语"保留——
  未来接入 TTS 引擎时，TTS 输出重定向到文件后即可复用此 sender 发送 record 段，无需改动主流程。
- url 通道：网络音频先同步下载到本地临时文件（规避 NapCat 10s 超时），再走 local_file 路径发送 record 段。
"""
from pathlib import Path

from ..napcat_client import NapCatClient
from ..media_downloader import download_to_local
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

    def send(self, group_id: str, voice_data: dict) -> bool:
        """发送 AI 语音。

        Args:
            group_id: 目标群号
            voice_data: 含 text 字段

        Returns:
            True=发送成功（含降级为 text 成功），False=彻底失败
        """
        text = voice_data.get("text", "")
        if not text:
            logger.warning("voice 段缺少 text，跳过")
            return False

        # send_group_ai_record 内部已通过 _call 检查业务 status，失败返回 {}
        result = self.client.send_group_ai_record(group_id, self.character, text)
        if result:
            logger.info(f"AI 语音发送成功: {text[:30]}...")
            return True

        # 发送失败
        logger.error(f"AI 语音发送失败: character={self.character} text={text[:30]}...")
        if self.fallback_to_text:
            logger.info("降级为 text 段发送（fallback_to_text=true）")
            resp = self.client.send_group_msg(group_id, [
                {"type": "text", "data": {"text": text}},
            ])
            return bool(resp)
        return False


class LocalFileVoiceSender:
    """本地音频文件发送器，构造 OB11MessageRecord 段。

    作为可复用原语保留：未来接入 TTS 引擎时，TTS 输出重定向到文件后，
    可直接复用此 sender 发送 record 段，无需改动主流程。
    """

    def __init__(self, client: NapCatClient):
        self.client = client

    def send(self, group_id: str, voice_data: dict) -> bool:
        """发送本地音频文件。

        Args:
            group_id: 目标群号
            voice_data: 含 file 字段（本地音频路径）

        Returns:
            True=发送成功，False=失败
        """
        file_path = voice_data.get("file", "")
        if not file_path or not Path(file_path).exists():
            logger.warning(f"voice 本地文件不存在: {file_path}")
            return False

        # 转成 file:// URI：NapCat 是独立进程，CWD 与本进程不同，相对路径无法解析
        file_uri = Path(file_path).resolve().as_uri()
        record_seg = [{"type": "record", "data": {"file": file_uri}}]
        resp = self.client.send_group_msg(group_id, record_seg)
        return bool(resp)


class UrlVoiceSender:
    """网络音频发送器：先下载到本地，再走 local_file 路径发送 record 段。

    NapCat 下载 URL 时有 10s 内部超时硬限，本 sender 先同步下载到本地临时文件
    （符合"真人模拟"设计——准备音频期间专注一事），再转 file:// URI 发送 record 段。
    """

    def __init__(self, client: NapCatClient, timeout: int = 30, temp_dir: str = "media/downloaded"):
        self.client = client
        self.timeout = timeout
        self.temp_dir = temp_dir
        self.local_sender = LocalFileVoiceSender(client)

    def send(self, group_id: str, voice_data: dict) -> bool:
        """发送网络音频。

        Args:
            group_id: 目标群号
            voice_data: 含 url 字段（网络音频地址）

        Returns:
            True=发送成功，False=失败（下载失败/发送失败）
        """
        url = voice_data.get("url", "")
        if not url:
            logger.warning("voice url 段缺少 url，跳过")
            return False

        # 网络通道：同步下载到本地，再走 local_file 路径
        file_path = download_to_local(url, timeout=self.timeout, temp_dir=self.temp_dir)
        if not file_path:
            logger.warning(f"音频下载失败，跳过发送: url={url[:80]}")
            return False

        # 复用 local_file 通道发送
        local_data = {"file": file_path}
        return self.local_sender.send(group_id, local_data)
