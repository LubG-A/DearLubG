"""大模型 API 调用客户端。"""
import json
import time
import requests
from typing import Optional

from .config import Config
from .utils.logger import get_logger

logger = get_logger("llm_client")


class LLMClient:
    """大模型 API 调用封装。

    使用标准多轮对话格式：
    messages = [
        {"role": "system", "content": "人格+协议+早期摘要"},
        {"role": "user", "content": "群消息批次1"},
        {"role": "assistant", "content": "LLM 返回的完整 JSON（含 thought，无论 silent 还是 reply）"},
        {"role": "user", "content": "群消息批次2"},
        {"role": "assistant", "content": "..."},
        ...
    ]
    """

    def __init__(self, config: Config):
        self.api_url = config.llm.api_url
        self.api_key = config.llm.api_key
        self.model = config.llm.model

    def chat(self, system_prompt: str, history_messages: list[dict], new_user_content: str) -> Optional[str]:
        """调用大模型，返回 assistant 的原始字符串内容。

        单次调用失败时重试 1 次（间隔 2 秒），两次都失败才返回 None。

        Args:
            system_prompt: 系统提示词（人格 + 协议 + 早期摘要）
            history_messages: 历史多轮对话（user/assistant 交替，不含 system）
            new_user_content: 本轮新增的 user 内容（群消息批次）

        Returns:
            LLM 返回的原始字符串内容（通常为 JSON 字符串），失败返回 None
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # 拼装完整 messages：system + 历史 + 本轮新 user
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": new_user_content})

        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
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
