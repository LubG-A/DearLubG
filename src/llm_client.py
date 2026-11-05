"""大模型 API 调用客户端。"""
import time
import requests
from typing import Optional

from .config import Config
from .vision_input import prepare_image_part
from .utils.logger import get_logger

logger = get_logger("llm_client")


class LLMClient:
    """大模型 API 调用封装。

    使用标准多轮对话格式：
    messages = [
        {"role": "system", "content": "人格+协议+长期记忆"},
        {"role": "user", "content": "群消息批次1"},
        {"role": "assistant", "content": "LLM 返回的完整行指令输出（含 state，无论 silent 还是 reply）"},
        {"role": "user", "content": "群消息批次2"},
        {"role": "assistant", "content": "..."},
        ...
    ]
    """

    # 历史压缩已改由主模型在正常对话/记忆整理 cycle 中自行写入记忆，
    # 不再使用独立的摘要 prompt（详见 src/memory.py）。

    def __init__(self, config: Config):
        self.api_url = config.llm.api_url
        self.api_key = config.llm.api_key
        self.model = config.llm.model
        self.vision = getattr(config, "vision", None)
        md = getattr(config, "media_download", None)
        self.media_temp_dir = md.temp_dir if md else "media/downloaded"

    def chat(self, system_prompt: str, history_messages: list[dict], new_user_content: str,
             images: Optional[list[dict]] = None) -> Optional[str]:
        """调用大模型，返回 assistant 的原始字符串内容。

        单次调用失败时重试 1 次（间隔 2 秒），两次都失败才返回 None。

        Args:
            system_prompt: 系统提示词（人格 + 协议 + 早期摘要）
            history_messages: 历史多轮对话（user/assistant 交替，不含 system）
            new_user_content: 本轮新增的 user 内容（群消息批次）
            images: 本轮消息中的图片源列表（NapCat image 段 data），
                    按 DeepSeek 多模态格式插入 user content 的 image_url 块。

        Returns:
            LLM 返回的原始字符串内容（行指令文本），失败返回 None
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # 拼装完整 messages：system + 历史 + 本轮新 user
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": self._build_user_content(new_user_content, images)})

        payload = {
            "model": self.model,
            "messages": messages,
        }

        # 最多重试 2 次（共 2 次请求）
        for attempt in range(2):
            try:
                resp = requests.post(self.api_url, headers=headers, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                logger.debug(f"LLM 返回: {content[:200]}")
                return content
            except requests.RequestException as e:
                if attempt == 0:
                    logger.warning(f"LLM 第 1 次请求失败: {e}，2 秒后重试")
                    time.sleep(2)
                else:
                    logger.error(f"LLM 第 2 次请求仍失败: {e}")
            except (KeyError, IndexError) as e:
                logger.error(f"LLM 响应解析失败: {e}")
                return None
        return None

    def _build_user_content(self, new_user_content: str, images: Optional[list[dict]]):
        """把图片源转成 OpenAI/DeepSeek 兼容的 content block 数组。

        无图或识图关闭时返回原字符串，保持与历史字符串消息一致。
        """
        if not images or not self.vision or not self.vision.enabled:
            return new_user_content

        limit = max(0, int(self.vision.max_images_per_turn))
        valid_sources = [s for s in images if isinstance(s, dict)]
        # 同一张图可能同时出现在当前消息和被引用消息中，按 url/path/file 去重
        unique_sources = []
        seen_keys = set()
        for src in valid_sources:
            key = str(src.get("url") or src.get("path") or src.get("file") or "")
            if key:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            unique_sources.append(src)

        total = len(unique_sources)
        selected = unique_sources[:limit]
        if total > len(selected):
            logger.info(f"识图：本轮 {total} 张图片，超过上限 {limit}，只取前 {len(selected)} 张")

        parts = []
        for src in selected:
            part = prepare_image_part(
                src,
                max_side=self.vision.max_side,
                detail=self.vision.detail,
                temp_dir=self.media_temp_dir,
            )
            if part:
                parts.append(part)

        if not parts:
            return new_user_content

        note = (
            f"[本批共 {total} 张图片，已附加 {len(parts)} 张，"
            f"按上方 [图片] 标记的出现顺序排列]"
        )
        # 把图片说明放在最终输出指令之前，保持"请按行指令输出。"在最后
        marker = "请按行指令输出。"
        if marker in new_user_content:
            text = new_user_content.replace(marker, f"{note}\n\n{marker}", 1)
        else:
            text = f"{new_user_content}\n\n{note}"
        logger.info(f"识图：{len(parts)} 张图片已插入本轮 user content")
        return [{"type": "text", "text": text}] + parts
