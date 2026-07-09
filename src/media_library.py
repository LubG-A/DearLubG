"""多媒体素材库加载器。

启动时一次性加载 media/manifest.json，渲染为 system prompt 片段（# 你的素材库 节）。
作为稳定能力注入 system prompt 的稳定部分（# 你的过往 之后、summary 之前），
不破坏前缀缓存——仅在 manifest 变更时断缓存一次，与 summary 压缩行为一致。

manifest.json 由人工维护，结构见 media/manifest.json。
"""
import json
from pathlib import Path

from .utils.logger import get_logger

logger = get_logger("media_library")


class MediaLibrary:
    """素材库加载与渲染。

    启动时加载一次 manifest.json，render_for_prompt 输出稳定文本片段。
    manifest 缺失/为空/损坏时静默返回空字符串，不阻塞启动，素材库功能关闭。
    """

    def __init__(self, manifest_path: str = "media/manifest.json"):
        self.manifest_path = Path(manifest_path)
        self.entries: dict[str, list[dict]] = {}  # {category: [entry, ...]}
        self._load()

    def _load(self):
        if not self.manifest_path.exists():
            logger.info(f"素材库清单不存在: {self.manifest_path}，素材库功能关闭")
            return
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"素材库清单加载失败: {e}，素材库功能关闭")
            return

        for cat in ("audio", "video", "image"):
            entries = data.get(cat, [])
            if not isinstance(entries, list):
                logger.warning(f"manifest.{cat} 不是列表，跳过该类")
                continue
            valid = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                if e.get("type") == "file" and e.get("path"):
                    valid.append(e)
                elif e.get("type") == "url" and e.get("url"):
                    valid.append(e)
                else:
                    logger.warning(f"素材条目无效（缺 type/path 或 type/url）: {e}")
            self.entries[cat] = valid

        total = sum(len(v) for v in self.entries.values())
        if total:
            logger.info(
                f"已加载素材库: audio={len(self.entries.get('audio', []))} "
                f"video={len(self.entries.get('video', []))} "
                f"image={len(self.entries.get('image', []))}"
            )
        else:
            logger.info("素材库清单为空，素材库功能关闭")

    def render_for_prompt(self) -> str:
        """渲染为 system prompt 片段。空库返回空字符串（不注入）。"""
        if not any(self.entries.values()):
            return ""

        lines = [
            "\n\n# 你的素材库（可选发送的多媒体文件）",
            "下面是你可以选择发送的多媒体文件清单，每条含路径或 URL 及内容描述。",
            "想发送某条时，把对应路径/URL 原样复制进 messages 段：",
            "- 语音（本地）：[{\"type\":\"voice\",\"data\":{\"channel\":\"local_file\",\"file\":\"<path>\"}}]",
            "- 语音（网络）：[{\"type\":\"voice\",\"data\":{\"channel\":\"url\",\"url\":\"<url>\"}}]",
            "- 图片（本地）：[{\"type\":\"image\",\"data\":{\"file\":\"<path>\"}}]",
            "- 图片（网络）：[{\"type\":\"image\",\"data\":{\"url\":\"<url>\"}}]",
            "- 视频（本地）：[{\"type\":\"video\",\"data\":{\"file\":\"<path>\"}}]",
            "- 视频（网络）：[{\"type\":\"video\",\"data\":{\"url\":\"<url>\"}}]",
            "只在语境自然合适时用，不要为用而用；与文字搭配时单独作为一条消息发送。",
        ]
        for cat, label in (("audio", "## 音频"), ("video", "## 视频"), ("image", "## 图片")):
            items = self.entries.get(cat, [])
            if not items:
                continue
            lines.append(label)
            for e in items:
                loc = e["path"] if e["type"] == "file" else e["url"]
                lines.append(f"- {loc} —— {e.get('desc', '')}")
        return "\n".join(lines)
