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
        {"role": "system", "content": "人格+协议+早期摘要"},
        {"role": "user", "content": "群消息批次1"},
        {"role": "assistant", "content": "LLM 返回的完整行指令输出（含 state，无论 silent 还是 reply）"},
        {"role": "user", "content": "群消息批次2"},
        {"role": "assistant", "content": "..."},
        ...
    ]
    """

    # 历史压缩用的 system prompt（方案A分层压缩）
    _SUMMARY_SYSTEM_PROMPT = (
        "你是对话摘要助手。把以下多轮对话压缩成要点，严格保留：\n"
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
        self.vision = getattr(config, "vision", None)
        md = getattr(config, "media_download", None)
        self.media_temp_dir = md.temp_dir if md else "media/downloaded"
        # 对话前缀续写使用 DeepSeek Beta 接口（普通接口不接受 prefix 字段）
        prefix_cfg = getattr(config, "prefix", None)
        self.prefix_api_url = prefix_cfg.beta_api_url if prefix_cfg else ""

    def chat(self, system_prompt: str, history_messages: list[dict], new_user_content: str,
             images: Optional[list[dict]] = None,
             assistant_prefix: Optional[str] = None) -> Optional[str]:
        """调用大模型，返回 assistant 的原始字符串内容。

        单次调用失败时重试 1 次（间隔 2 秒），两次都失败才返回 None。

        Args:
            system_prompt: 系统提示词（人格 + 协议 + 早期摘要）
            history_messages: 历史多轮对话（user/assistant 交替，不含 system）
            new_user_content: 本轮新增的 user 内容（群消息批次）
            images: 本轮消息中的图片源列表（NapCat image 段 data），
                    按 DeepSeek 多模态格式插入 user content 的 image_url 块。
            assistant_prefix: 对话前缀续写（/pf）。非空时作为最后一条 assistant
                             消息（prefix=True）发给 Beta 接口，返回值为"前缀+续写"。

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

        url = self.api_url
        if assistant_prefix:
            # DeepSeek 对话前缀续写：最后一条必须是 assistant，并标记 prefix=True
            messages.append({"role": "assistant", "content": assistant_prefix, "prefix": True})
            url = self._prefix_url()
            logger.info(f"使用 /pf 前缀续写：前缀 {len(assistant_prefix)} 字，接口 {url}")

        payload = {
            "model": self.model,
            "messages": messages,
        }

        # 最多重试 2 次（共 2 次请求）
        for attempt in range(2):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                logger.debug(f"LLM 返回: {content[:200]}")
                if assistant_prefix:
                    # 接口只返回续写部分；拼回前缀，后续 parser 才能看到完整行指令
                    if content.startswith(assistant_prefix):
                        return content
                    return assistant_prefix + content
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

    def _prefix_url(self) -> str:
        """解析前缀续写接口地址：优先配置，否则在普通 URL 中插入 /beta。"""
        if self.prefix_api_url:
            return self.prefix_api_url
        if "/chat/completions" in self.api_url:
            return self.api_url.replace("/chat/completions", "/beta/chat/completions", 1)
        return self.api_url

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
            role = "对方消息" if m["role"] == "user" else "LubG回复"
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
