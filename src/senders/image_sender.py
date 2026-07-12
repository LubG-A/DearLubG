"""图片发送器实现。

支持双通道：
- 本地图片（data.file 字段）：构造 OB11MessageImage 段，file 字段转 file:// URI。
- 网络图片（data.url 字段）：先同步下载到本地临时文件（规避 NapCat 10s 超时），再走 file 通道。

配合素材库 manifest 使用：LLM 从 system prompt 的素材库描述选取路径/URL，
原样输出到 image 段 data.file/data.url，本 sender 透传给 NapCat。
"""
from pathlib import Path

from ..napcat_client import NapCatClient
from ..media_downloader import download_to_local
from ..utils.logger import get_logger

logger = get_logger("image_sender")


class NapCatImageSender:
    """NapCat 图片发送器，实现 ImageSender 协议。

    按 image_data 是否含 url 字段决定走网络下载通道，否则走本地 file 通道。
    网络通道先下载到本地（同步阻塞，符合"真人模拟"设计），再转 file:// URI 发送。
    """

    def __init__(self, client: NapCatClient, timeout: int = 30, temp_dir: str = "media/downloaded"):
        self.client = client
        self.timeout = timeout
        self.temp_dir = temp_dir

    def send(self, group_id: str, image_data: dict) -> bool:
        """发送图片。

        Args:
            group_id: 目标群号
            image_data: 含 url（网络）或 file（本地）字段

        Returns:
            True=发送成功，False=失败（下载失败/发送失败）
        """
        url = image_data.get("url", "")
        file_path = image_data.get("file", "")

        if url:
            # 网络通道：同步下载到本地，再走 file:// URI
            file_path = download_to_local(url, timeout=self.timeout, temp_dir=self.temp_dir)
            if not file_path:
                logger.warning(f"图片下载失败，跳过发送: url={url[:80]}")
                return False

        if not file_path or not Path(file_path).exists():
            logger.warning(f"image 本地文件不存在且无 url: file={file_path}")
            return False

        # 转成 file:// URI：NapCat 是独立进程，CWD 与本进程不同，相对路径无法解析
        file_uri = Path(file_path).resolve().as_uri()
        seg = [{"type": "image", "data": {"file": file_uri}}]
        resp = self.client.send_group_msg(group_id, seg)
        return bool(resp)
