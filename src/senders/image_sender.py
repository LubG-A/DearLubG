"""图片发送器实现。

支持双通道：
- 本地图片（data.file 字段）：构造 OB11MessageImage 段，file 字段为本地路径。
- 网络图片（data.url 字段）：构造 OB11MessageImage 段，url 字段为网络地址。

配合素材库 manifest 使用：LLM 从 system prompt 的素材库描述选取路径/URL，
原样输出到 image 段 data.file/data.url，本 sender 透传给 NapCat。
"""
from pathlib import Path

from ..napcat_client import NapCatClient
from ..utils.logger import get_logger

logger = get_logger("image_sender")


class NapCatImageSender:
    """NapCat 图片发送器，实现 ImageSender 协议。

    按 image_data 是否含 url 字段决定走网络通道，否则走本地 file 通道。
    """

    def __init__(self, client: NapCatClient):
        self.client = client

    def send(self, group_id: str, image_data: dict) -> dict:
        """发送图片。

        Args:
            group_id: 目标群号
            image_data: 含 url（网络）或 file（本地）字段

        Returns:
            NapCat 返回的响应字典；失败返回 {}
        """
        url = image_data.get("url", "")
        if url:
            seg = [{"type": "image", "data": {"url": url}}]
            return self.client.send_group_msg(group_id, seg)

        file_path = image_data.get("file", "")
        if not file_path or not Path(file_path).exists():
            logger.warning(f"image 本地文件不存在且无 url: file={file_path}")
            return {}

        # 转成 file:// URI：NapCat 是独立进程，CWD 与本进程不同，相对路径无法解析
        file_uri = Path(file_path).resolve().as_uri()
        seg = [{"type": "image", "data": {"file": file_uri}}]
        return self.client.send_group_msg(group_id, seg)
