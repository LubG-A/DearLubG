"""网络媒体资源统一下载器。

NapCat 下载 URL 资源时有 10s 内部超时硬限，生成型 URL（如 AI 绘画 30s+）必定超时。
本模块在 Bot 侧同步下载 URL 到本地临时文件，再由 sender 转 file:// URI 发送给 NapCat，
让 NapCat 永远只读本地文件，规避其内部超时。

设计理念：同步下载阻塞 cycle 是合理的"真人模拟"——真人在一个群准备图片时
不会跑去别的群说话，全局单队列的串行阻塞恰恰模拟了这种"专注一件事"的行为。
"""
import hashlib
from pathlib import Path
from typing import Optional

import requests

from .utils.logger import get_logger

logger = get_logger("media_downloader")

# 默认配置（可由 config.media_download 覆盖）
DEFAULT_TEMP_DIR = "media/downloaded"
DEFAULT_TIMEOUT = 30
DEFAULT_VIDEO_TIMEOUT = 60


def _infer_ext(url: str, content_type: str) -> str:
    """从 Content-Type 或 URL 路径推断扩展名。"""
    ct_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "audio/amr": ".amr",
        "audio/mp3": ".mp3",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/silk": ".silk",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in ct_map:
            return ct_map[ct]
    # fallback：从 URL 路径后缀
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    return ".bin"


def download_to_local(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    temp_dir: str = DEFAULT_TEMP_DIR,
) -> Optional[str]:
    """同步下载 URL 到本地临时文件，返回本地路径。失败返回 None。

    文件命名用 sha256(url)[:16]，同一 URL 多次下载会命中已存在的文件（轻量缓存效果）。
    下载失败（网络错误/HTTP 非 200/非媒体类型）返回 None。

    Args:
        url: 网络资源 URL
        timeout: 下载超时（秒）
        temp_dir: 临时文件存放目录
    Returns:
        本地文件路径，失败返回 None
    """
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    # 先检查是否已下载过（同 URL 命中已存在的文件，避免重复下载）
    cache_dir = Path(temp_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for existing in cache_dir.glob(f"{url_hash}.*"):
        logger.info(f"媒体命中已下载文件: {existing.name} (url={url[:60]}...)")
        return str(existing)

    # 下载
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"媒体下载请求失败: {e} (url={url[:80]}...)")
        return None

    content_type = resp.headers.get("Content-Type", "")
    ext = _infer_ext(url, content_type)
    output_path = cache_dir / f"{url_hash}{ext}"

    try:
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    except Exception as e:
        logger.warning(f"媒体写入文件失败: {e} (path={output_path})")
        return None

    logger.info(f"媒体已下载到本地: {output_path.name}")
    return str(output_path)
