"""图片发送器（空实现）。

当前阶段为空实现，后续完成 image 系统后再替换为真实实现。
主流程通过 ImageSender 接口依赖，替换时零修改。
"""
from ..utils.logger import get_logger

logger = get_logger("image_sender")


class EmptyImageSender:
    """图片发送器空实现。"""

    def send(self, group_id: str, image_data: dict) -> dict:
        """空实现：仅记日志，抛 NotImplementedError。"""
        url = image_data.get("url", "")
        summary = image_data.get("summary", "")
        logger.info(f"[空实现] 图片发送被跳过 url={url} summary={summary}")
        raise NotImplementedError("ImageSender 尚未实现，等待 image 系统完成")
