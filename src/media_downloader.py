"""网络媒体资源统一下载器。

NapCat 下载 URL 资源时有 10s 内部超时硬限，生成型 URL（如 AI 绘画 30s+）必定超时。
本模块在 Bot 侧同步下载 URL 到本地临时文件，再由 sender 转 file:// URI 发送给 NapCat，
让 NapCat 永远只读本地文件，规避其内部超时。

设计理念：同步下载阻塞 cycle 是合理的"真人模拟"——真人在一个群准备图片时
不会跑去别的群说话，全局单队列的串行阻塞恰恰模拟了这种"专注一件事"的行为。
"""
import hashlib
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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
        "image/avif": ".avif",
        "image/heic": ".heic",
        "image/heif": ".heif",
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


def _normalize_url(url: str) -> str:
    """规范化 URL：对空格等特殊字符做百分号编码，保留 URL 结构字符。

    LLM 输出的 URL 可能含空格（如 prompt 未完全编码），requests.get 无法处理。
    quote 保留 URL 结构字符（:/?&=,+;%@）和已编码的 %XX，仅对非法字符编码。
    """
    return quote(url, safe=':/?&=,+;%@')


def download_to_local(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    temp_dir: str = DEFAULT_TEMP_DIR,
    expected_size: Optional[int] = None,
) -> Optional[str]:
    """同步下载 URL 到本地临时文件，返回本地路径。失败返回 None。

    文件命名用 sha256(url)[:16]，同一 URL 多次下载会命中已存在的文件（轻量缓存效果）。
    下载先写 .part 临时文件，校验大小完整后再原子改名为正式文件；失败时删除 .part，
    避免把连接中断（IncompleteRead）留下的残片当成缓存命中。

    Args:
        url: 网络资源 URL
        timeout: 下载超时（秒）
        temp_dir: 临时文件存放目录
        expected_size: 期望文件大小（字节），用于校验缓存与下载完整性；None 表示不校验
    Returns:
        本地文件路径，失败返回 None
    """
    # 规范化 URL：确保空格等特殊字符被正确编码（在算 hash 前规范化，确保缓存一致性）
    url = _normalize_url(url)
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    # 先检查是否已下载过（同 URL 命中已存在的文件，避免重复下载）
    cache_dir = Path(temp_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for existing in cache_dir.glob(f"{url_hash}.*"):
        if existing.suffix == ".part":
            continue
        if expected_size and existing.stat().st_size != expected_size:
            logger.warning(
                f"缓存文件大小不符，删除后重下: {existing.name} "
                f"({existing.stat().st_size} != {expected_size})"
            )
            existing.unlink(missing_ok=True)
            break
        logger.info(f"媒体命中已下载文件: {existing.name} (url={url[:60]}...)")
        return str(existing)

    part_path = cache_dir / f"{url_hash}.part"
    content_type = ""

    # 最多尝试 2 次：中断后用 Range 从已写入位置续传（服务端不支持则整段重下）
    for attempt in range(2):
        offset = part_path.stat().st_size if part_path.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with requests.get(url, timeout=timeout, stream=True, headers=headers) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                append = resp.status_code == 206 and offset > 0
                if not append:
                    offset = 0
                total = offset + int(resp.headers.get("Content-Length", 0) or 0)
                with open(part_path, "ab" if append else "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                written = part_path.stat().st_size
                if total and written < total:
                    raise IOError(f"下载不完整: {written}/{total} bytes")
                if expected_size and written != expected_size:
                    raise IOError(f"文件大小不符: {written}/{expected_size} bytes")
            break
        except Exception as e:
            logger.warning(f"媒体下载失败(第 {attempt + 1} 次): {e} (url={url[:80]}...)")
            if attempt == 0:
                time.sleep(0.5)
                continue
            part_path.unlink(missing_ok=True)
            return None

    output_path = cache_dir / f"{url_hash}{_infer_ext(url, content_type)}"
    part_path.replace(output_path)
    logger.info(f"媒体已下载到本地: {output_path.name}")
    return str(output_path)
