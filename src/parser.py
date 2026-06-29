"""LLM 输出 JSON 解析与校验模块。

严格校验，解析失败 fallback silent，绝不把脏数据发到群里。
"""
import json
from typing import Optional
from dataclasses import dataclass, field

from .utils.logger import get_logger

logger = get_logger("parser")

VALID_ACTIONS = {"silent", "reply", "react", "multi_reply"}
VALID_MSG_TYPES = {"text", "at", "reply", "face", "image", "voice", "forward"}


@dataclass
class ParsedResult:
    """解析后的结果。"""
    thought: str = ""
    action: str = "silent"
    targets: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    react_emoji_id: str = ""
    react_target_msg_id: str = ""
    delay_seconds: int = 0
    affinity_delta: dict = field(default_factory=dict)
    reply_delay_minutes: int = 0  # 延迟回复：N 分钟后再回这批消息


def parse_and_validate(raw_content: str) -> ParsedResult:
    """解析并校验 LLM 输出。

    Args:
        raw_content: LLM 返回的原始字符串

    Returns:
        ParsedResult，解析失败返回 action=silent 的默认结果
    """
    if not isinstance(raw_content, str):
        logger.error(f"raw_content 非 str 类型: {type(raw_content)}，fallback silent")
        return ParsedResult(action="silent")

    # LLM 返回可能有前缀空白字符或 markdown 代码块包裹，先清理
    stripped = raw_content.strip()
    if stripped.startswith("```"):
        # 去除 markdown 代码块
        lines = stripped.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"JSON 解析失败: {e}，raw={stripped[:200]}，fallback silent")
        return ParsedResult(action="silent")

    # action 校验
    action = data.get("action", "silent")
    if action not in VALID_ACTIONS:
        logger.error(f"非法 action={action}，fallback silent")
        return ParsedResult(action="silent")

    # messages 校验
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        logger.error("messages 非数组，fallback silent")
        return ParsedResult(action="silent")

    # messages 长度与 action 匹配
    # reply / multi_reply 均允许 1-10 条（真人连发短消息场景）
    if action in ("reply", "multi_reply") and not (1 <= len(messages) <= 10):
        logger.error(f"{action} 需 1-10 条消息，实际 {len(messages)}，fallback silent")
        return ParsedResult(action="silent")
    if action in ("silent", "react") and len(messages) != 0:
        logger.warning(f"{action} 应 0 条消息，实际 {len(messages)}，清空")
        messages = []

    # 消息段 type 校验
    # messages 数组每个元素 = 一条消息，支持三种形式：
    #   - str：纯文本简写，等价于 [{"type":"text","data":{"text":...}}]
    #   - dict：单段消息简写，等价于 [seg]
    #   - list：多段混合消息（如 text+face 一条发出），元素为 dict 段
    cleaned_messages = []
    for msg in messages:
        if isinstance(msg, str):
            cleaned_messages.append(msg)
            continue
        if isinstance(msg, list):
            # list 形式：一条消息含多段（混合消息）
            # list 内部元素支持 string（text 简写）和 dict（结构化段）
            segs = []
            for seg in msg:
                if isinstance(seg, str):
                    seg = {"type": "text", "data": {"text": seg}}
                cleaned_seg = _validate_segment(seg)
                if cleaned_seg is not None:
                    segs.append(cleaned_seg)
            if segs:
                cleaned_messages.append(segs)
            else:
                logger.warning(f"混合消息所有段无效，丢弃: {msg}")
            continue
        if not isinstance(msg, dict):
            logger.warning(f"消息非 str/dict/list，丢弃: {msg}")
            continue
        cleaned_seg = _validate_segment(msg)
        if cleaned_seg is not None:
            cleaned_messages.append(cleaned_seg)

    # affinity_delta 校验
    affinity_delta = data.get("affinity_delta", {})
    if not isinstance(affinity_delta, dict):
        affinity_delta = {}
    cleaned_delta = {}
    for qq, delta in affinity_delta.items():
        if not isinstance(delta, (int, float)):
            continue
        # clip 到 [-2, +2]
        cleaned_delta[str(qq)] = max(-2, min(2, delta))

    # delay_seconds 校验
    delay = data.get("delay_seconds", 0)
    if not isinstance(delay, (int, float)) or delay < 0:
        delay = 0
    delay = min(delay, 15)

    # reply_delay_minutes 校验（延迟回复）
    # 仅当 action=silent 时有效：表示"已读但稍后回"，不是真的 silent
    reply_delay = data.get("reply_delay_minutes", 0)
    if not isinstance(reply_delay, (int, float)) or reply_delay < 0:
        reply_delay = 0
    reply_delay = min(int(reply_delay), 120)  # 上限 2 小时，避免过长

    # 语义校验：reply_delay_minutes 仅在 silent 时有意义
    # 若 action != silent 但有 reply_delay_minutes，忽略（按 reply/react 正常处理）
    final_reply_delay = reply_delay if action == "silent" else 0

    # react_target_msg_id 校验（非空 str，与 user content 的 [#msg_id] 标记一致）
    # 仅 action=react 时需要，其他 action 不校验（LLM 无需输出该字段）
    react_id = data.get("react_target_msg_id", "")
    if not isinstance(react_id, str):
        react_id = str(react_id) if react_id else ""
    if action == "react" and not react_id:
        logger.warning("react_target_msg_id 为空，react 段将无法定位消息")

    return ParsedResult(
        thought=data.get("thought", ""),
        action=action,
        targets=data.get("targets", []),
        messages=cleaned_messages,
        react_emoji_id=str(data.get("react_emoji_id", "")),
        react_target_msg_id=react_id,
        delay_seconds=int(delay),
        affinity_delta=cleaned_delta,
        reply_delay_minutes=final_reply_delay,
    )


def _validate_segment(seg) -> Optional[dict]:
    """校验单个消息段（dict 形式）。

    Args:
        seg: 待校验的段，应为 dict

    Returns:
        校验通过的段 dict，或 None（无效段丢弃）
    """
    if not isinstance(seg, dict):
        logger.warning(f"消息段非 dict，丢弃: {seg}")
        return None
    msg_type = seg.get("type", "")
    if msg_type not in VALID_MSG_TYPES:
        logger.warning(f"未知消息段 type={msg_type}，丢弃")
        return None
    # reply 段：校验 target_msg_id（非空 str，由 sender 配合 history 透传给 NapCat 校验）
    if msg_type == "reply":
        data_dict = seg.get("data", {})
        if not isinstance(data_dict, dict):
            data_dict = {}
        target_id = data_dict.get("target_msg_id", "")
        if not isinstance(target_id, str):
            target_id = str(target_id) if target_id else ""
        if not target_id:
            logger.warning("reply 段 target_msg_id 为空，丢弃 reply 段")
            return None
        data_dict["target_msg_id"] = target_id
        seg["data"] = data_dict
    return seg
