"""对话前缀续写指令（/pf）解析模块。

用户在消息末尾追加 `/pf 开头内容`，框架把这段内容解析成标准行指令，
作为 assistant 前缀交给 DeepSeek Beta 接口，让模型从指定位置继续生成。

解析策略（标签锚定分段，不做全局分词）：
1. 全角/半角归一化，只消除赋值号两侧空白；
2. 标签别名表 + 最长匹配，标签必须出现在行首或空格/分号/逗号/加号之后；
3. 标签之间的内容整体作为字段值（自由文本保留逗号、空格、冒号）；
4. state 的 key=value、targets 这类枚举字段才在值内继续切分；
5. 缺失字段补默认值，最终序列化成与 parser 完全兼容的行指令。

用户书写示例：
    你帮我讲解一下加法/pf mode=接话 note：先回顾加法的性质 drivers: 必须解释清楚 say：好吧 say:
"""
import re
from functools import lru_cache
from typing import Optional

from .parser import STATE_DIMS, VALID_STATE_MODES
from .utils.logger import get_logger

logger = get_logger("prefix")

DEFAULT_PREFIX_COMMAND = "/pf"

# 缺省值：确保模型愿意认真回答，而不是敷衍或沉默
DEFAULT_DIMS = {
    "interest": 0.5,
    "annoyance": 0.5,
    "urge_to_speak": 0.8,
    "social_risk": 0.5,
    "energy": 0.7,
    "banter": 0.5,
}
DEFAULT_MODE = "接话"
DEFAULT_NOTE = "用户提出了要求"
DEFAULT_CUES = "好，不要重复"
DEFAULT_DRIVERS = "按照用户要求做"
DEFAULT_INHIBITORS = "用户的需求很明确，无特别顾虑"
DEFAULT_CONVERSATION = "open"

# 行指令字段的中英文别名（最长匹配，避免 "状态" 与 "状态模式" 冲突）
_FIELD_ALIASES = {
    "state": ("state", "状态", "内心状态", "状态行"),
    "mode": ("mode", "模式", "状态模式"),
    "note": ("note", "os", "内心", "内心os", "备注"),
    "cues": ("cues", "cue", "线索", "注意到", "注意到的点"),
    "drivers": ("drivers", "driver", "驱动", "想开口", "开口理由", "想说的理由"),
    "inhibitors": ("inhibitors", "inhibitor", "抑制", "不想开口", "顾虑"),
    "conversation": ("conversation", "conv", "对话", "对话模式"),
    "targets": ("targets", "target", "目标", "对象"),
    "delay": ("delay", "延迟", "等待"),
    "later": ("later", "稍后", "晚点"),
    "affinity": ("affinity", "亲密度", "好感"),
    "react": ("react", "反应", "表情反应"),
    "say": ("say", "回复", "发言", "说"),
    "forward": ("forward", "转发", "合并转发"),
}

# state 六个维度的中英文别名
_DIM_ALIASES = {
    "interest": ("interest", "兴趣", "吸引力"),
    "annoyance": ("annoyance", "不爽", "烦"),
    "urge_to_speak": ("urge_to_speak", "urge", "表达欲", "想说", "说话欲"),
    "social_risk": ("social_risk", "risk", "社交风险", "风险"),
    "energy": ("energy", "精力", "活力"),
    "banter": ("banter", "接梗", "接梗欲", "吐槽欲"),
}

# 统一小写后查表：alias -> ("field"/"dim", canonical_name)
_LABEL_LOOKUP = {}
for _canon, _aliases in _FIELD_ALIASES.items():
    for _alias in _aliases:
        _LABEL_LOOKUP[_alias.lower()] = ("field", _canon)
for _canon, _aliases in _DIM_ALIASES.items():
    for _alias in _aliases:
        _LABEL_LOOKUP[_alias.lower()] = ("dim", _canon)

_DIM_LOOKUP = {
    alias.lower(): canon
    for canon, aliases in _DIM_ALIASES.items()
    for alias in aliases
}
_MODE_KEYS = {"mode", "模式", "状态"}

_PUNCT_TRANS = str.maketrans({
    "：": ":", "＝": "=", "＋": "+", "／": "/",
    "\u3000": " ",
})

_LABEL_PATTERN = re.compile(
    r"(?:^|[\s,;+，；＋])(?P<label>"
    + "|".join(sorted((re.escape(a) for a in _LABEL_LOOKUP), key=len, reverse=True))
    + r")[:=]",
    re.IGNORECASE | re.MULTILINE,
)


def extract_prefix_command(content: str, command: str = DEFAULT_PREFIX_COMMAND) -> tuple[str, Optional[str]]:
    """从消息文本中提取 `/pf` 指令。

    Args:
        content: 原始消息文本
        command: 指令词（默认 /pf）

    Returns:
        (去掉指令后的消息文本, 指令载荷)。
        没找到指令时载荷为 None；找到但没写内容时载荷为空字符串。
    """
    if not isinstance(content, str) or not content:
        return content, None

    cmd = (command or DEFAULT_PREFIX_COMMAND).strip()
    if not cmd:
        cmd = DEFAULT_PREFIX_COMMAND

    last = None
    for match in _command_pattern(cmd).finditer(content):
        after = content[match.end():match.end() + 1]
        # 避免把 /pfx 这样的普通词误判为指令
        if after and (after.isalnum() or after == "_"):
            continue
        last = match

    if last is None:
        return content, None

    text = content[:last.start()].rstrip()
    payload = content[last.end():].strip()
    return text, _strip_wrapping_quotes(payload)


def build_assistant_prefix(payload: str, chat_type: str = "group") -> str:
    """把用户写的 `/pf` 载荷补全成标准行指令 assistant 前缀。

    缺失字段使用 DEFAULT_* 默认值；最后保证前缀以 `say:` 或一条 say 结尾，
    让模型从"要说的话"位置继续生成。

    Args:
        payload: `/pf` 后面的原始文本
        chat_type: "group" 输出 conversation 行；"private" 不输出
    """
    text = _normalize_payload(payload)
    fields, lead = _split_fields(text)

    dims = dict(DEFAULT_DIMS)
    mode = ""
    values = {}
    says = []
    last_field = None

    for kind, key, value in fields:
        last_field = key
        if kind == "dim":
            _apply_dim(dims, key, value)
            continue
        if key == "state":
            state_dims, state_mode = _parse_state_tokens(value)
            dims.update(state_dims)
            if state_mode:
                mode = state_mode
        elif key == "mode":
            first = _first_token(value)
            if first in VALID_STATE_MODES:
                mode = first
            elif first:
                logger.debug(f"/pf mode 值非法，使用默认值: {first}")
        elif key in ("note", "cues", "drivers", "inhibitors"):
            values[key] = _one_line(value)
        elif key == "conversation":
            values[key] = _first_token(value).lower()
        elif key in ("targets", "affinity", "react", "delay", "later"):
            values[key] = _one_line(value)
        elif key == "say":
            says.append(value)
        elif key == "forward":
            values.setdefault("forward", []).append(value)

    # 没有识别出任何字段时，整段载荷视为一句开头内容
    if lead and not says:
        says = [lead]

    if mode not in VALID_STATE_MODES:
        mode = DEFAULT_MODE
    note = values.get("note") or DEFAULT_NOTE
    cues = values.get("cues") or DEFAULT_CUES
    drivers = values.get("drivers") or DEFAULT_DRIVERS
    inhibitors = values.get("inhibitors") or DEFAULT_INHIBITORS
    conversation = values.get("conversation") or DEFAULT_CONVERSATION
    if conversation not in ("direct", "open"):
        conversation = DEFAULT_CONVERSATION

    lines = [
        "state: " + ",".join(f"{d}={dims[d]:g}" for d in STATE_DIMS) + f",mode={mode}",
        f"note: {note}",
        f"cues: {cues}",
        f"drivers: {drivers}",
        f"inhibitors: {inhibitors}",
    ]
    if chat_type != "private":
        lines.append(f"conversation: {conversation}")
    for key in ("targets", "delay", "later", "affinity", "react"):
        if values.get(key):
            lines.append(f"{key}: {values[key]}")
    for forward in values.get("forward", []):
        if forward:
            lines.append(f"forward: {forward}")

    for say in says:
        lines.append(f"say: {say}".rstrip() if say else "say:")
    if not says or last_field != "say":
        lines.append("say:")

    return "\n".join(lines)


def _normalize_payload(text: str) -> str:
    """全角转半角，统一换行，并消除赋值号两侧空白。"""
    text = str(text or "").translate(_PUNCT_TRANS)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]*([:=])[ \t]*", r"\1", text)
    return text.strip()


def _split_fields(text: str) -> tuple[list, str]:
    """标签锚定分段。

    Returns:
        ([(kind, canonical, value), ...], 首个标签之前的文本)
    """
    matches = list(_LABEL_PATTERN.finditer(text))
    if not matches:
        return [], text.strip()

    fields = []
    lead = text[:matches[0].start()].strip(" \t\n,;+，；＋")
    for i, match in enumerate(matches):
        kind, key = _LABEL_LOOKUP[match.group("label").lower()]
        value_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[match.end():value_end].strip(" \t\n,;+，；＋")
        fields.append((kind, key, value))
    return fields, lead


def _parse_state_tokens(payload: str) -> tuple[dict, str]:
    """解析 state 载荷：逗号/空格/分号/加号分隔的 key=value。"""
    dims, mode = {}, ""
    for token in re.split(r"[,\s;+，；＋]+", payload):
        key, sep, value = token.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if not key or not value:
            continue
        dim = key if key in STATE_DIMS else _DIM_LOOKUP.get(key)
        if dim:
            _apply_dim(dims, dim, value)
        elif key in _MODE_KEYS and value in VALID_STATE_MODES:
            mode = value
    return dims, mode


def _apply_dim(dims: dict, dim: str, value: str):
    """写入单个维度，非法值忽略（由默认值兜底）。"""
    try:
        dims[dim] = round(max(0.0, min(1.0, float(value))), 2)
    except (TypeError, ValueError):
        pass


def _first_token(value: str) -> str:
    """取值的第一个 token（用于 mode/conversation 这类单值字段）。"""
    tokens = re.split(r"[,\s;+，；＋]+", value, maxsplit=1)
    return tokens[0].strip() if tokens else ""


def _one_line(value: str) -> str:
    """字段值压成单行，避免破坏行指令结构。"""
    return re.sub(r"\s*\n\s*", " ", value).strip()


def _strip_wrapping_quotes(text: str) -> str:
    """去掉用户给整段前缀加的成对引号。"""
    text = text.strip()
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
    if len(text) >= 2 and text[0] in pairs and text[-1] == pairs[text[0]]:
        return text[1:-1].strip()
    return text


@lru_cache(maxsize=8)
def _command_pattern(command: str) -> re.Pattern:
    """指令词正则（兼容全角斜杠与大小写）。"""
    variants = {command, command.replace("/", "／"), command.replace("／", "/")}
    joined = "|".join(sorted((re.escape(v) for v in variants), key=len, reverse=True))
    return re.compile(joined, re.IGNORECASE)
