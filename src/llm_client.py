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

    # 历史压缩用的 system prompt（方案A分层压缩）
    _SUMMARY_SYSTEM_PROMPT = (
        "你是对话摘要助手。把以下群聊多轮对话压缩成要点，严格保留：\n"
        "1. 谁和 LubG 有过互动/冲突（带昵称和 QQ 号尾号）\n"
        "2. 话题脉络（聊了什么主题、有什么结论或未结的话题）\n"
        "3. LubG 表达过的立场/承诺/口头禅使用情况\n"
        "4. 重要的情绪节点（谁的语气让 LubG 不爽/开心）\n"
        "5. 保留消息的 [#msg_id] 标记（如有），便于后续引用和撤回定位\n"
        "每轮对话压缩成 1-2 句话，整体控制在 400 字以内。\n"
        "只输出摘要正文，不要任何前缀或解释。"
    )

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

    def summarize(self, messages_to_compress: list[dict]) -> Optional[str]:
        """调用 LLM 把一段历史对话压缩成摘要文本（方案A分层压缩用）。

        单次调用，失败返回 None（由调用方决定是否放弃压缩）。

        Args:
            messages_to_compress: 待压缩的 user/assistant 对话列表

        Returns:
            摘要文本字符串，失败返回 None
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # 把对话列表拼成可读文本喂给 LLM
        lines = []
        for m in messages_to_compress:
            role = "群消息" if m["role"] == "user" else "LubG回复"
            # user content 可能很长（含群成员列表头部），只取后 300 字（消息正文部分）
            content = m["content"][-300:] if len(m["content"]) > 300 else m["content"]
            lines.append(f"[{role}] {content}")
        dialog_text = "\n".join(lines)

        messages = [
            {"role": "system", "content": self._SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"请压缩以下对话：\n\n{dialog_text}"},
        ]

        payload = {
            "model": self.model,
            "messages": messages,
        }

        try:
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            logger.info(f"LLM 历史摘要生成成功，长度 {len(content)} 字")
            return content.strip()
        except requests.RequestException as e:
            logger.warning(f"LLM 历史摘要请求失败: {e}")
            return None
        except (KeyError, IndexError) as e:
            logger.warning(f"LLM 历史摘要响应解析失败: {e}")
            return None
