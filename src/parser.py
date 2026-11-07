"""LLM 输出解析与校验模块（P005 行指令协议）。

协议见 P005_返回结构行指令协议方案.md：
- 一行一条指令，格式 `指令: 载荷`；
- 指令词固定白名单，前缀匹配，不用通用正则；
- 一行坏只丢一行，绝不把脏数据发到群里。
"""
import re
from dataclasses import dataclass, field

from .utils.logger import get_logger

logger = get_logger("parser")

COMMANDS = ("state", "note", "cues", "drivers", "inhibitors", "journal", "core", "memo", "forget",
            "conversation", "targets", "delay", "later", "affinity", "react", "say", "forward")
# 可续行的自由文本指令
FREE_TEXT_CMDS = ("note", "cues", "drivers", "inhibitors")
# 可续行的指令：say 续写拼回上一条；core 续写拼成下一行（一行一个话题）
CONTINUABLE_CMDS = ("say", "note", "cues", "drivers", "inhibitors", "core")
# 内部记忆指令：只写记忆，不发送到聊天；落历史时会被剥离，避免历史里堆内部指令
INTERNAL_COMMANDS = ("journal", "core", "memo", "forget")
# 单条指令的保守上限（真实上限由 MemoryManager 按配置截断，这里防止异常超长载荷）
JOURNAL_PAYLOAD_MAX = 2000
CORE_PAYLOAD_MAX = 4000
MEMO_PAYLOAD_MAX = 1000
# 阶段 G：结构化内心状态的固定维度（全部 clip 到 [0,1]）
STATE_DIMS = ("interest", "annoyance", "urge_to_speak", "social_risk", "energy", "banter")
VALID_STATE_MODES = {"旁观", "接话", "吐槽", "补刀", "起哄", "主动开口"}
SEG_TAGS = {"at", "reply", "face", "poke", "img", "video", "voice_ai", "voice_file", "voice_url"}


@dataclass
class ParsedResult:
    """解析后的结果。"""
    state: dict = field(default_factory=dict)  # 阶段 G：结构化内心状态（仅观测/记忆，不参与决策）
    action: str = "silent"
    targets: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    react_emoji_id: str = ""
    react_target_msg_id: str = ""
    delay_seconds: int = 0
    affinity_delta: dict = field(default_factory=dict)
    reply_delay_minutes: int = 0  # 延迟回复：N 分钟后再回这批消息
    conversation_mode: str = "open"  # 阶段 F：direct=与某人往返对话 / open=开放插话
    # 记忆系统：内部指令，不发送到聊天
    journal: list = field(default_factory=list)      # 新增日记条目
    core_update: str = ""                             # 记忆整理时的长期记忆全量替换
    memos: list = field(default_factory=list)         # 新增短期备忘
    forget_ids: list = field(default_factory=list)    # 删除的备忘编号


def parse_and_validate(raw_content: str) -> ParsedResult:
    """解析行指令输出。

    逐行解析，未知/非法行只丢该行；解析不出任何有效内容时返回 silent。
    """
    result = ParsedResult()
    if not isinstance(raw_content, str):
        logger.error(f"raw_content 非 str 类型: {type(raw_content)}，fallback silent")
        return result

    messages = []
    forward_idx = None
    last_free = None  # 上一条可续行的指令：say / note / cues / drivers / inhibitors

    def close_forward():
        nonlocal forward_idx
        if forward_idx is not None:
            # 没有 > 子行的 forward 整条丢弃
            if not messages[forward_idx][0]["data"]["messages"]:
                messages.pop(forward_idx)
            forward_idx = None

    for raw in raw_content.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 容错：模型偶尔会包 markdown 代码块
        if line.startswith("```"):
            continue

        # forward 的内容行（紧跟在 forward 之后）
        if forward_idx is not None:
            if line.startswith(">"):
                content = line[1:].strip()
                if content:
                    messages[forward_idx][0]["data"]["messages"].append(
                        {"type": "text", "data": {"text": content}}
                    )
                continue
            close_forward()

        # forward 之外的游离 > 行：无效，丢弃并切断续行
        if line.startswith(">"):
            logger.warning(f"forward 之外的 > 行，跳过: {line[:40]}")
            last_free = None
            continue

        parsed = _split_command(line)
        if parsed is None:
            if last_free == "core" and result.core_update:
                # core 多行续写优先：话题行可能自带英文冒号（如 "AI: ..."），
                # 不能当未知指令丢掉；保留换行，供记忆整理按"一行一个话题"存储
                result.core_update = (result.core_update + "\n" + line).strip()
            elif _looks_like_command(line):
                logger.warning(f"未知指令，跳过: {line[:40]}")
                last_free = None
            elif last_free == "say" and messages:
                if messages[-1] and messages[-1][-1].get("type") == "text":
                    prev = messages[-1][-1]["data"]["text"].rstrip()
                    messages[-1][-1]["data"]["text"] = prev + " " + line
                else:
                    messages[-1].append({"type": "text", "data": {"text": line}})
            elif last_free in FREE_TEXT_CMDS:
                result.state[last_free] = (result.state.get(last_free, "") + " " + line).strip()
            else:
                logger.warning(f"无法识别的行，跳过: {line[:40]}")
            continue

        cmd, payload = parsed
        last_free = cmd if cmd in CONTINUABLE_CMDS else None

        if cmd == "state":
            keep = {k: result.state[k] for k in FREE_TEXT_CMDS if k in result.state}
            result.state = _parse_state_line(payload)
            result.state.update(keep)
        elif cmd in FREE_TEXT_CMDS:
            result.state.setdefault("dims", {d: 0.5 for d in STATE_DIMS})
            limit = 120 if cmd == "cues" else 60
            result.state[cmd] = payload[:limit]
        elif cmd == "journal":
            text = payload.strip()
            if text and len(result.journal) < 3:
                result.journal.append(text[:JOURNAL_PAYLOAD_MAX])
        elif cmd == "core":
            text = payload.strip()
            if text:
                result.core_update = text[:CORE_PAYLOAD_MAX]
        elif cmd == "memo":
            text = payload.strip()
            if text and len(result.memos) < 3:
                result.memos.append(text[:MEMO_PAYLOAD_MAX])
        elif cmd == "forget":
            # 只删除备忘；日记由代码按时间窗自动淘汰
            result.forget_ids = [int(x) for x in re.findall(r"\d+", payload)][:20]
        elif cmd == "conversation":
            result.conversation_mode = payload if payload in ("direct", "open") else "open"
        elif cmd == "targets":
            result.targets = [x.strip() for x in payload.split(",") if x.strip()]
        elif cmd == "delay":
            try:
                result.delay_seconds = max(0, min(15, int(payload)))
            except ValueError:
                result.delay_seconds = 0
        elif cmd == "later":
            try:
                result.reply_delay_minutes = max(0, min(120, int(payload)))
            except ValueError:
                result.reply_delay_minutes = 0
        elif cmd == "affinity":
            parts = [x.strip() for x in payload.split(",")]
            if len(parts) == 2 and parts[0]:
                try:
                    result.affinity_delta[parts[0]] = max(-2, min(2, float(parts[1])))
                except ValueError:
                    pass
        elif cmd == "react":
            parts = [x.strip() for x in payload.split(",")]
            if len(parts) == 2 and parts[0] and parts[1]:
                result.react_target_msg_id, result.react_emoji_id = parts
        elif cmd == "say":
            segs = _parse_segments(payload)
            if segs:
                messages.append(segs)
        elif cmd == "forward":
            seg = {"type": "forward", "data": {"title": payload, "messages": []}}
            messages.append([seg])
            forward_idx = len(messages) - 1

    close_forward()

    # note/cues 等先于 state 时，state 行会补齐 dims；这里兜底"只有自由文本"的情况
    if result.state and "dims" not in result.state:
        result.state["dims"] = {d: 0.5 for d in STATE_DIMS}
    if result.core_update:
        result.core_update = result.core_update[:CORE_PAYLOAD_MAX]

    # action 由内容派生（模型不再输出 action）
    if messages:
        messages = messages[:10]
        result.action = "reply" if len(messages) == 1 else "multi_reply"
        result.reply_delay_minutes = 0
        result.react_target_msg_id = ""
        result.react_emoji_id = ""
    elif result.reply_delay_minutes > 0:
        result.action = "silent"
        result.react_target_msg_id = ""
        result.react_emoji_id = ""
    elif result.react_target_msg_id and result.react_emoji_id:
        result.action = "react"
    else:
        result.action = "silent"
    result.messages = messages
    return result


def _split_command(line: str):
    """固定词表前缀匹配，不用通用正则。返回 (cmd, payload) 或 None。"""
    for cmd in COMMANDS:
        if not line.startswith(cmd):
            continue
        rest = line[len(cmd):]
        if rest[:1] in (":", "："):
            return cmd, rest[1:].strip()
        if rest[:1].isspace():
            rest = rest.strip()
            if rest[:1] in (":", "："):
                rest = rest[1:]
            return cmd, rest.strip()
    return None


def _looks_like_command(line: str) -> bool:
    """行首是 ASCII 单词 + 冒号，但不是已知指令 → 判定为未知指令。"""
    for sep in (":", "："):
        if sep in line:
            head = line.split(sep, 1)[0].strip()
            return bool(head) and head.isascii() and head.replace("_", "").isalpha()
    return False


def _parse_state_line(payload: str) -> dict:
    """解析 state 载荷：逗号/空格分隔的 key=value，dims clip 到 [0,1]。"""
    s = payload.replace("＝", "=").replace("，", ",")
    s = re.sub(r"\s*=\s*", "=", s)
    dims, mode = {}, ""
    for tok in re.split(r"[,\s]+", s):
        if not tok:
            continue
        key, sep, val = tok.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key in STATE_DIMS:
            try:
                dims[key] = round(max(0.0, min(1.0, float(val))), 2)
            except ValueError:
                pass
        elif key == "mode" and val in VALID_STATE_MODES:
            mode = val
    return {"dims": {d: dims.get(d, 0.5) for d in STATE_DIMS}, "mode": mode, "note": ""}


def _parse_segments(payload: str) -> list:
    """扫描 say 载荷：已知标签转段，其余原样当文本（空白保真）。"""
    segs, i = [], 0
    while i < len(payload):
        j = payload.find("[", i)
        if j < 0:
            _add_text(segs, payload[i:])
            break
        if j > i:
            _add_text(segs, payload[i:j])
        k = payload.find("]", j)
        if k < 0:  # 没闭合：剩余全部当文本
            _add_text(segs, payload[j:])
            break
        inner = payload[j + 1:k]
        name, sep, val = inner.partition(":")
        name = name.strip()
        if not sep or name not in SEG_TAGS:
            _add_text(segs, payload[j:k + 1])
        else:
            status, seg = _build_segment(name, val.strip())
            if status == "ok":
                segs.append(seg)
        i = k + 1
    return segs


def _add_text(segs: list, text: str):
    """跳过空串；段内空格原样保留（整个 payload 已在切指令时 strip）。"""
    if text:
        segs.append({"type": "text", "data": {"text": text}})


def _build_segment(name: str, val: str):
    """构造单个段。返回 (ok/drop/literal, seg)。

    - ok：合法段
    - drop：已知标签但值非法 → 整段丢弃
    - literal：未知标签 → 调用方原样当文本
    """
    if name == "at":
        return ("ok", {"type": "at", "data": {"qq": val}}) if val.isdigit() else ("drop", None)
    if name == "reply":
        return ("ok", {"type": "reply", "data": {"target_msg_id": val}}) if val else ("drop", None)
    if name == "face":
        return ("ok", {"type": "face", "data": {"id": val}}) if val else ("drop", None)
    if name == "poke":
        return ("ok", {"type": "poke", "data": {"qq": val}}) if val.isdigit() else ("drop", None)
    if name in ("img", "video"):
        if not val:
            return ("drop", None)
        typ = "image" if name == "img" else "video"
        data = {"url": val} if val.startswith(("http://", "https://")) else {"file": val}
        return ("ok", {"type": typ, "data": data})
    if name == "voice_ai":
        return ("ok", {"type": "voice", "data": {"text": val, "channel": "ai_record"}}) if val else ("drop", None)
    if name == "voice_file":
        return ("ok", {"type": "voice", "data": {"channel": "local_file", "file": val}}) if val else ("drop", None)
    if name == "voice_url":
        return ("ok", {"type": "voice", "data": {"channel": "url", "url": val}}) if val else ("drop", None)
    return ("literal", None)


_INTERNAL_LINE_RE = re.compile(r"^\s*(journal|core|memo|forget)(\s*[:：]|\s+)", re.IGNORECASE)


def strip_internal_commands(raw_content: str) -> str:
    """剥离 assistant 输出中的内部记忆指令（落历史前调用）。

    记忆由专门的 core/journal/memo 块呈现；如果把这些指令原样留进历史，
    既会重复占用 token，也会让模型看到一串陈旧的备忘。
    内部指令的续行（如 core 的多行话题）也一并剥离，直到遇见下一条指令。
    state/note/cues/drivers/inhibitors 等内心状态仍然保留，用于人格连续性。
    """
    if not isinstance(raw_content, str):
        return ""
    lines = []
    skipping = False
    for line in raw_content.splitlines():
        if _INTERNAL_LINE_RE.match(line):
            skipping = True
            continue
        if skipping:
            parsed = _split_command(line.strip())
            if parsed is None:
                # 不是新指令，视为内部指令的续行，跳过
                continue
            skipping = False
            if parsed[0] in INTERNAL_COMMANDS:
                continue
        lines.append(line)
    return "\n".join(lines).strip()
