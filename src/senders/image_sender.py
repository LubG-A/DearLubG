"""图片发送器实现。

支持双通道：
- 本地图片（data.file 字段）：构造 OB11MessageImage 段，file 字段转 file:// URI。
- 网络图片（data.url 字段）：先同步下载到本地临时文件（规避 NapCat 10s 超时），再走 file 通道。

配合素材库 manifest 使用：LLM 从 system prompt 的素材库描述选取路径/URL，
通过行指令 `say: [img:路径或URL]` 输出，parser 转成 image 段后由本 sender 发送。

格式兼容：QQ 富媒体不支持 AVIF/HEIC 等新格式（NapCat 会报 "rich media transfer failed"），
发送前用 Pillow 检查，必要时转成 PNG。
"""
from pathlib import Path

from ..napcat_client import NapCatClient
from ..media_downloader import download_to_local
from ..utils.logger import get_logger

logger = get_logger("image_sender")

# QQ/NapCat 可直接发送的常见图片格式（Pillow 的 format 名称）
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "GIF", "WEBP", "BMP"}


def _ensure_supported_image(file_path: str) -> str:
    """确保图片格式可被 NapCat/QQ 发送。

    AVIF/HEIC 等格式不支持时用 Pillow 转成 PNG（同目录缓存，避免重复转换）。
    Pillow 缺失或转换失败返回空串，调用方跳过发送。
    """
    path = Path(file_path)
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow 未安装，无法检查/转换非常见图片格式（建议安装 Pillow）")
        return file_path

    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            if fmt in SUPPORTED_IMAGE_FORMATS:
                return file_path
            converted = path.with_suffix(".png")
            if not converted.exists():
                im.convert("RGB").save(converted, "PNG")
                logger.info(f"图片格式 {fmt} 不受 QQ 支持，已转换为 PNG: {converted.name}")
            else:
                logger.info(f"命中已转换的 PNG: {converted.name}")
            return str(converted)
    except Exception as e:
        logger.warning(f"图片格式检查/转换失败，跳过发送: {e} (file={file_path})")
        return ""


class NapCatImageSender:
    """NapCat 图片发送器，实现 ImageSender 协议。

    按 image_data 是否含 url 字段决定走网络下载通道，否则走本地 file 通道。
    网络通道先下载到本地（同步阻塞，符合"真人模拟"设计），再转 file:// URI 发送。
    """

    def __init__(self, client: NapCatClient, timeout: int = 30, temp_dir: str = "media/downloaded"):
        self.client = client
        self.timeout = timeout
        self.temp_dir = temp_dir

    def send(self, chat_type: str, chat_id: str, image_data: dict) -> bool:
        """发送图片。

        Args:
            chat_type: "group" 或 "private"
            chat_id: 群号或私聊对方 QQ
            image_data: 含 url（网络）或 file（本地）字段

        Returns:
            True=发送成功，False=失败（下载失败/格式不支持/发送失败）
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

        # 格式兼容：AVIF/HEIC 等 NapCat/QQ 不支持的格式转 PNG
        file_path = _ensure_supported_image(file_path)
        if not file_path:
            logger.warning(f"图片格式不受支持且转换失败，跳过发送: file={image_data.get('file', '')}")
            return False

        # 转成 file:// URI：NapCat 是独立进程，CWD 与本进程不同，相对路径无法解析
        file_uri = Path(file_path).resolve().as_uri()
        seg = [{"type": "image", "data": {"file": file_uri}}]
        resp = self.client.send_message(chat_type, chat_id, seg)
        return bool(resp)
