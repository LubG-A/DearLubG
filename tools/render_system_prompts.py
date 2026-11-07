"""系统提示词 / 完整对话渲染工具。

三种视图：
- prompt：只渲染 system prompt（人格、规则、素材库、长期记忆）。
- conversation：渲染 system prompt + conversation.json 里持久化的 user/assistant
  消息（JSON 里的 \\n 会还原成真实换行），并在文末附一份当前 journal/memo 注入示例。
- consolidation：记忆整理轮次的 system prompt + 整理用 user content
  （现有 core / journal / memo + 整理要求）。

conversation.json 本身就是 LLM 看到的 messages 结构，只是按 JSON 分块存储；
本工具只做展开和排版，不重新拼装运行时的 cycle。

用法：
    python tools/render_system_prompts.py                        # 三种视图都写
    python tools/render_system_prompts.py --target prompt        # 只渲染 system prompt
    python tools/render_system_prompts.py --target conversation  # 只渲染完整对话
    python tools/render_system_prompts.py --target consolidation # 只渲染记忆整理轮次
    python tools/render_system_prompts.py --stdout               # 直接打印
    python tools/render_system_prompts.py --chat group
    python tools/render_system_prompts.py --group-id 720021466

输出文件：
    tools/output/{scene}_{chat_id}.txt                  # system prompt
    tools/output/{scene}_{chat_id}.conversation.txt     # 完整对话
    tools/output/{scene}_{chat_id}.memory_consolidation.txt  # 记忆整理轮次
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.memory import MemoryManager
from src.persona import PersonaRenderer


def _read_core(path: Path) -> str:
    """读取长期记忆；文件不存在时返回空串（首次启动 / 未整理过记忆）。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_conversation(path: Path) -> dict:
    """读取 conversation.json；缺失/损坏时返回空结构。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _render_entries(entries: list) -> str:
    """把消息结构渲染成 user content 里的消息行（与 HistoryManager.render_messages 一致）。"""
    lines = []
    for m in entries or []:
        if not isinstance(m, dict):
            continue
        prefix = "[bot]" if m.get("is_bot") else ""
        msg_id = m.get("msg_id", "")
        msg_id_tag = f"[#{msg_id}]" if (not m.get("is_bot") and msg_id) else ""
        delayed = "  [延迟回复]" if m.get("is_delayed") else ""
        lines.append(
            f"{msg_id_tag} [{m.get('time', '')}] {prefix}"
            f"{m.get('nickname', '')}({m.get('qq', '')}): {m.get('content', '')}{delayed}"
        )
    return "\n".join(lines)


def _collect_targets(cfg, chat_filter: str, group_filter: str) -> list:
    """按配置列出要渲染的 chat。"""
    targets = []

    if chat_filter in ("all", "group"):
        for gid in cfg.napcat.group_ids:
            gid = str(gid)
            if group_filter and gid != group_filter:
                continue
            state_dir = ROOT / "state" / gid
            targets.append({
                "scene": "group",
                "chat_type": "group",
                "chat_id": gid,
                "state_dir": state_dir,
                "core_path": state_dir / "memory" / "core.md",
                "conversation_path": state_dir / "conversation.json",
            })

    if chat_filter in ("all", "private"):
        private = getattr(cfg, "private", None)
        if private and private.enabled:
            for qq in getattr(private, "allow_list", []):
                qq = str(qq)
                state_dir = ROOT / "state" / "private" / qq
                targets.append({
                    "scene": "private",
                    "chat_type": "private",
                    "chat_id": qq,
                    "state_dir": state_dir,
                    "core_path": state_dir / "memory" / "core.md",
                    "conversation_path": state_dir / "conversation.json",
                })

    return targets


def _header(target: dict, generated_at: str, extra: list) -> str:
    try:
        core_display = target["core_path"].relative_to(ROOT).as_posix()
    except ValueError:
        core_display = str(target["core_path"])
    lines = [
        f"# scene: {target['scene']}",
        f"# chat_id: {target['chat_id']}",
        f"# generated_at: {generated_at}",
        f"# core_memory: {core_display}（{len(_read_core(target['core_path']))} 字）",
        *extra,
    ]
    lines.append("=" * 80)
    return "\n".join(lines)


def _render_prompt(renderer: PersonaRenderer, target: dict, generated_at: str) -> tuple:
    """只渲染 system prompt。"""
    core = _read_core(target["core_path"])
    prompt = renderer.render_system_prompt(core_memory=core, chat_type=target["chat_type"])
    text = _header(target, generated_at, [f"# prompt_chars: {len(prompt)}"]) + "\n" + prompt + "\n"
    meta = {"filename": f"{target['scene']}_{target['chat_id']}.txt", "chars": len(prompt)}
    return meta["filename"], text, meta


def _render_conversation(renderer: PersonaRenderer, cfg, target: dict, generated_at: str) -> tuple:
    """渲染完整对话：system prompt + conversation.json 的 messages + 记忆示例。"""
    core = _read_core(target["core_path"])
    system_prompt = renderer.render_system_prompt(core_memory=core, chat_type=target["chat_type"])

    data = _load_conversation(target["conversation_path"])
    messages = [m for m in data.get("messages", []) if isinstance(m, dict)]
    recent_blocks = [b for b in data.get("recent_blocks", []) if isinstance(b, dict)]
    pending = [m for m in data.get("pending_group_msgs", []) if isinstance(m, dict)]
    delayed = [d for d in data.get("delayed_replies", []) if isinstance(d, dict)]

    unpersisted_entries = []
    for block in recent_blocks:
        entries = block.get("entries")
        if isinstance(entries, list):
            unpersisted_entries.extend(e for e in entries if isinstance(e, dict))
    unpersisted_entries.extend(pending)

    turn_count = len(messages) // 2
    extra = [
        f"# source: {target['conversation_path'].relative_to(ROOT).as_posix()}",
        f"# messages: {len(messages)}（{turn_count} 轮 user/assistant）",
        f"# not_in_messages: recent_blocks={len(recent_blocks)}, pending={len(pending)}, "
        f"delayed={len(delayed)}",
    ]

    parts = [_header(target, generated_at, extra), f"======================== [SYSTEM] ========================\n{system_prompt}"]

    for i, m in enumerate(messages, 1):
        role = m.get("role", "unknown")
        content = m.get("content", "")
        parts.append(f"---------------- [{i}] {role} ----------------\n{content}")

    if unpersisted_entries:
        parts.append(
            "======================== [CURRENT] user（尚未写入 messages 的最近消息，实际会拼进当前 user 块） ========================\n"
            + _render_entries(unpersisted_entries)
        )

    if delayed:
        delayed_lines = ["======================== [DELAYED] 未到期的延迟回复（不在 messages 中） ========================\n"]
        for item in delayed:
            delayed_lines.append(
                f"- due={item.get('due_time', '')}，messages={len(item.get('messages', []))} 条"
            )
        parts.append("\n".join(delayed_lines))

    # 当前 journal / memo 示例：落盘历史时会省略，但每轮 user content 实际会带
    memory = MemoryManager(cfg, str(target["state_dir"]))
    if getattr(memory, "enabled", False):
        memo_block = memory.render_memo_block() or "（空）"
        journal_block = memory.render_journal_block() or "（空）"
        appendix = (
            "======================== [APPENDIX] 当前记忆注入示例（历史 user 块中已省略） ========================\n"
            "实际请求中，以下两块位于「群成员/对方信息」之后、「最近消息」之前；\n"
            "写入 conversation.json 时会省略（for_storage=True），因此上面的历史 user 块里看不到。\n\n"
            "# 你的备忘录（短期提醒，只给自己看，不要复述）\n"
            f"{memo_block}\n\n"
            "# 你记得的事（过去的记忆，不是当前消息，不要复述）\n"
            f"{journal_block}"
        )
    else:
        appendix = "======================== [APPENDIX] 记忆功能已关闭（config.memory.enabled=false） ========================\n"
    parts.append(appendix)

    text = "\n\n".join(parts) + "\n"
    meta = {"filename": f"{target['scene']}_{target['chat_id']}.conversation.txt",
            "chars": len(text), "messages": len(messages), "turns": turn_count}
    return meta["filename"], text, meta


_SELF_RE = re.compile(r"- 你的昵称：(.+)（QQ：(\d+)）")
_MEMBER_HEADER = "群成员（昵称/QQ/角色/亲密度）："
_MEMBER_RE = re.compile(r"(.+)\((\d+)\) role=(\w+) affinity=([0-9.]+)")
_PEER_RE = re.compile(r"- 对方：(.+)\((\d+)\) 亲密度=([0-9.]+)")


def _example_context(messages: list, chat_type: str) -> tuple:
    """从最后一条 user 消息里提取自己/成员/对方/消息正文，用于渲染整理示例。"""
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    content = str(last_user.get("content", ""))

    self_nick, self_qq = "", ""
    match = _SELF_RE.search(content)
    if match:
        self_nick, self_qq = match.group(1), match.group(2)

    member_list = []
    idx = content.find(_MEMBER_HEADER)
    if idx >= 0:
        started = False
        for line in content[idx + len(_MEMBER_HEADER):].splitlines():
            line = line.strip()
            if not line:
                if started:
                    break
                continue
            started = True
            match = _MEMBER_RE.match(line)
            if match:
                member_list.append({
                    "nickname": match.group(1),
                    "qq": match.group(2),
                    "role": match.group(3),
                    "affinity": float(match.group(4)),
                })

    history_context = "（示例：暂无未读消息）"
    for marker in ("# 最近群消息", "# 最近消息"):
        j = content.find(marker)
        if j < 0:
            continue
        body = content[j:]
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.replace("\n\n请按行指令输出。", "").replace("请按行指令输出。", "").strip()
        if body:
            history_context = body
        break

    peer = None
    if chat_type == "private":
        match = _PEER_RE.search(content)
        if match:
            peer = {
                "nickname": match.group(1),
                "qq": match.group(2),
                "affinity": float(match.group(3)),
            }
    return self_nick, self_qq, member_list, peer, history_context


def _render_consolidation(renderer: PersonaRenderer, cfg, target: dict, generated_at: str) -> tuple:
    """渲染记忆整理轮次：system prompt + 整理专用 user content。

    与 main._run_memory_cycle 一致：system prompt 带长期记忆，user content 由
    render_memory_consolidation 生成。实际请求还会附带 conversation.json 的历史消息，
    这里不重复展开，只在文件末尾说明（历史可用 --target conversation 查看）。
    """
    core = _read_core(target["core_path"])
    system_prompt = renderer.render_system_prompt(core_memory=core, chat_type=target["chat_type"])

    memory = MemoryManager(cfg, str(target["state_dir"]))
    journal_text = memory.render_journal_for_consolidation()
    memo_text = memory.render_memo_block()
    user_content = renderer.render_memory_consolidation(core, journal_text, memo_text)

    data = _load_conversation(target["conversation_path"])
    messages = [m for m in data.get("messages", []) if isinstance(m, dict)]
    history_messages = len(messages)
    journal_entries = len([l for l in journal_text.splitlines() if l.strip()])

    # 历史接近上限时的整理要求：嵌在普通 user 块里（据最后一条 user 消息构造示例）
    self_nick, self_qq, member_list, peer, history_context = _example_context(
        messages, target["chat_type"])
    threshold_user = renderer.render_user_content(
        history_context, member_list,
        self_nick or cfg.persona.name, self_qq or "示例QQ",
        is_active=False, chat_type=target["chat_type"], peer=peer,
        journal=memory.render_journal_block(), memo=memo_text,
        memory_consolidation=True,
    )

    extra = [
        f"# journal_entries: {journal_entries}（整理时看全部日记）",
        f"# history_messages: {history_messages}（实际请求会一并带上，见 conversation 视图）",
    ]
    parts = [
        _header(target, generated_at, extra),
        f"## [SYSTEM]\n{system_prompt}",
        f"## [CONSOLIDATION] user（定时整理：独立一轮，不回复聊天）\n{user_content}",
        f"## [CONSOLIDATION-THRESHOLD] user（历史接近上限：整理要求嵌在普通 user 块里，示例）\n"
        f"{threshold_user}",
        "## [NOTE]\n"
        "两种整理触发方式共用同一套 system prompt 与核心/日记/备忘内容：\n"
        "- 定时整理：`_run_memory_cycle` 直接以 [CONSOLIDATION] 作为 user 消息，不回复聊天。\n"
        "- 阈值整理：普通 user 块里额外出现 `# 记忆整理（本轮先更新记忆，再决定是否回复）`，"
        "模型先输出记忆、再决定是否说话。\n"
        "实际请求还会附带 conversation.json 的历史消息，这里不重复展开。",
    ]
    text = "\n\n".join(parts) + "\n"
    meta = {
        "filename": f"{target['scene']}_{target['chat_id']}.memory_consolidation.txt",
        "chars": len(text),
        "journal_entries": journal_entries,
    }
    return meta["filename"], text, meta


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 system prompt / 完整对话 / 记忆整理")
    parser.add_argument("--target", choices=("all", "prompt", "conversation", "consolidation"),
                        default="all", help="渲染内容，默认 all")
    parser.add_argument("--chat", choices=("all", "group", "private"), default="all",
                        help="渲染范围，默认 all")
    parser.add_argument("--group-id", default="", help="只渲染指定群（配合 --chat group/all）")
    parser.add_argument("--out", default=str(ROOT / "tools" / "output"),
                        help="输出目录，默认 tools/output/")
    parser.add_argument("--stdout", action="store_true", help="不写文件，直接打印到终端")
    args = parser.parse_args()

    cfg = load_config(str(ROOT / "config.yaml"))
    renderer = PersonaRenderer(
        cfg,
        state_dir=str(ROOT / "state"),
        media_manifest=str(ROOT / "media" / "manifest.json"),
    )

    targets = _collect_targets(cfg, args.chat, args.group_id)
    if not targets:
        print("没有可渲染的 chat（检查 config.yaml 的 group_ids / private 配置）")
        return 1

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_dir = Path(args.out)
    if not args.stdout:
        out_dir.mkdir(parents=True, exist_ok=True)

    rendered = 0
    for target in targets:
        jobs = []
        if args.target in ("all", "prompt"):
            jobs.append(_render_prompt(renderer, target, generated_at))
        if args.target in ("all", "conversation"):
            jobs.append(_render_conversation(renderer, cfg, target, generated_at))
        if args.target in ("all", "consolidation"):
            jobs.append(_render_consolidation(renderer, cfg, target, generated_at))

        for filename, text, meta in jobs:
            rendered += 1
            if args.stdout:
                print(text)
            else:
                (out_dir / filename).write_text(text, encoding="utf-8")
                if "messages" in meta:
                    print(
                        f"[{target['scene']}] {target['chat_id']} -> {out_dir / filename} "
                        f"（{meta['messages']} 条消息 / {meta['turns']} 轮，{meta['chars']} 字）"
                    )
                elif "journal_entries" in meta:
                    print(f"[{target['scene']}] {target['chat_id']} -> {out_dir / filename} "
                          f"（记忆整理，journal {meta['journal_entries']} 条，{meta['chars']} 字）")
                else:
                    print(f"[{target['scene']}] {target['chat_id']} -> {out_dir / filename} "
                          f"（prompt {meta['chars']} 字）")

    if not args.stdout:
        print(f"\n共渲染 {rendered} 个文件，输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
