"""识图输入准备：把聊天中的图片转成 DeepSeek 多模态请求内容块。

DeepSeek 文档要点：
- 支持 JPEG / PNG / GIF / WebP，格式按文件实际内容判断
- 图片只能出现在 user 消息的 content block 数组中
- 可以用 http(s) URL 或 base64 data URL
- 单图最大 32 MiB，请求体最大 48 MiB

本模块统一把图片转成本地 base64 data URL：
1. 优先用 NapCat 提供的本地 path/file；否则用 url 下载到 media/downloaded；
2. 用 Pillow 检查实际格式，AVIF/HEIC/BMP 等转 PNG；
3. 超过 max_side 的图片等比缩小，控制请求体大小；
4. 输出 OpenAI 兼容的 {"type": "image_url", "image_url": {...}} 内容块。
"""
import base64
import io
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

from .media_downloader import download_to_local
from .utils.logger import get_logger

logger = get_logger("vision_input")

# DeepSeek 支持的图片格式（Pillow format 名称 -> MIME）
SUPPORTED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

# 单图 base64 前的安全上限（DeepSeek 单图上限 32 MiB，这里留余量）
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def _local_path_from_value(value: str) -> Optional[str]:
    """把 path/file 字段解析成本地路径（支持 file:// URI）。"""
    if not value:
        return None
    if value.startswith("file://"):
        parsed = urlparse(value)
        path = url2pathname(parsed.path)
        # Windows: url2pathname('/D:/x') 可能得到 '\D:\x'，去掉前导反斜杠
        if path.startswith("\\") and len(path) > 2 and path[2] == ":":
            path = path[1:]
    else:
        path = value
    return path if Path(path).exists() else None


def resolve_image_path(source: dict, temp_dir: str) -> Optional[str]:
    """把一条图片源解析为本地文件路径。失败返回 None。"""
    for key in ("path", "file"):
        value = source.get(key, "")
        if isinstance(value, str):
            p = _local_path_from_value(value)
            if p:
                return p

    url = source.get("url", "")
    if not url:
        # NapCat 某些版本把 URL 放在 file 字段
        f = source.get("file", "")
        if isinstance(f, str) and f.startswith(("http://", "https://")):
            url = f
    if url:
        return download_to_local(url, temp_dir=temp_dir)
    return None


def _has_alpha(im) -> bool:
    """图片是否含透明通道。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def prepare_image_part(
    source: dict,
    max_side: int = 1280,
    detail: str = "auto",
    temp_dir: str = "media/downloaded",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Optional[dict]:
    """把一条图片源转成 DeepSeek image_url 内容块。

    Args:
        source: NapCat image 段 data，含 url/file/path
        max_side: 最长边上限，超过则等比缩小
        detail: low / high / original / auto
        temp_dir: 网络图片下载目录
        max_bytes: base64 前的图片字节上限

    Returns:
        {"type": "image_url", "image_url": {"url": "data:...", "detail": ...}}
        失败返回 None（调用方跳过该图，文本照常发送）。
    """
    path = resolve_image_path(source, temp_dir)
    if not path:
        logger.warning(f"识图图片无法定位或下载: {source}")
        return None

    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("Pillow 未安装，无法准备识图图片")
        return None

    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            width, height = im.size
            need_convert = fmt not in SUPPORTED_FORMATS
            need_resize = max(width, height) > max_side

            if not need_convert and not need_resize:
                # 已是支持格式且尺寸合适，直接编码原文件
                data = Path(path).read_bytes()
                mime = SUPPORTED_FORMATS[fmt]
            else:
                # 统一处理：纠正 EXIF 方向 → 缩小 → 按透明通道选 PNG/JPEG
                im = ImageOps.exif_transpose(im)
                im.thumbnail((max_side, max_side), Image.LANCZOS)
                if _has_alpha(im):
                    buf = io.BytesIO()
                    im.save(buf, "PNG", optimize=True)
                    data = buf.getvalue()
                    mime = "image/png"
                else:
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
                    data = buf.getvalue()
                    mime = "image/jpeg"
                logger.info(f"识图图片已处理: {Path(path).name} {fmt} {width}x{height} -> {mime}")

        if len(data) > max_bytes:
            logger.warning(f"识图图片过大，跳过: {len(data)} bytes > {max_bytes} (file={path})")
            return None

        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{b64}",
                "detail": detail,
            },
        }
    except Exception as e:
        logger.warning(f"识图图片处理失败，跳过: {e} (file={path})")
        return None
