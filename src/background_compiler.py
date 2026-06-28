"""背景故事总结脚本。

用户在 state/background_story.md 编写主人公的背景故事（可长可短），
本脚本调用 LLM 把故事总结成对群聊表现有指导价值的要点，
写入 state/background_summary.json（含 source_mtime 用于失效检测）。

persona 渲染时读取 background_summary.json，插入到系统提示词的"# 你的过往"节。

用法：
    python -m src.background_compiler           # 总结（mtime 变化才调用 LLM）
    python -m src.background_compiler --force    # 强制重新总结
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from .config import Config, load_config
from .llm_client import LLMClient
from .utils.logger import get_logger

logger = get_logger("background_compiler")

# 专门针对背景故事的总结 prompt
# 不做"故事梗概"，而是提取对群聊表现有指导价值的要点
_BACKGROUND_SUMMARY_PROMPT = """你是一个角色背景提取器。下面给你一段角色的背景故事，请提取出对该角色在群聊场景下的表现有指导价值的要点。

【输出要求】
1. 只输出要点，不要前缀、不要解释、不要"以下是总结"之类的开场白
2. 控制在 300-800 字以内
3. 用简洁的陈述句，不要列点编号
4. 按以下维度组织（用空行分段，不要写小标题）：

【提取维度】
- 性格细节：故事里展现的具体性格表现，补充而非重复"性格关键词"。例如"对陌生人警惕但熟了会暴露话痨属性"这种具体表现，而不是简单说"外冷内热"
- 人际态度：对不同类型人的态度（长辈/同龄/陌生人/特定群体），倾向亲近还是疏离，对哪类话题敏感
- 经历塑造的习惯/偏好：故事中提到的具体经历带来的具体偏好或回避点。例如"因为某次失败所以讨厌被催促""因为某段经历所以喜欢深夜聊天"
- 可在聊天自然提及的趣事/槽点：3-5 个可以在闲聊中自然带出的具体细节（不要硬塞，但要让 LLM 知道有这些素材可用）
- 禁忌或雷区：可能让角色不爽、回避、爆发的话题或行为
- 情绪触发点：什么场景下容易情绪波动（开心/生气/emo）

【注意事项】
- 不要重复 persona 已有的基础字段（姓名/性别/年龄/职业/兴趣等），只补充经历性内容
- 不要写具体 QQ 号关系（避免与 affinity 系统冲突），可以写"曾有个一起打游戏的朋友消失"这种泛指
- 不要写"她的故事告诉我们..."这种旁白，全程用第三人称客观陈述
- 如果故事原文与 persona 字段冲突，以 persona 为准，故事部分只取不冲突的细节

【故事原文】
{story}
"""


def compile_background(story_path: Path, summary_path: Path, llm: LLMClient, force: bool = False) -> bool:
    """总结背景故事，写入 summary_path。

    Args:
        story_path: 故事原文路径（state/background_story.md）
        summary_path: 总结缓存路径（state/background_summary.json）
        llm: LLM 客户端
        force: True 则强制重新总结（忽略 mtime）

    Returns:
        True 表示生成新总结，False 表示跳过（mtime 未变）
    """
    if not story_path.exists():
        logger.warning(f"背景故事文件不存在: {story_path}")
        return False

    story_text = story_path.read_text(encoding="utf-8").strip()
    if not story_text:
        logger.warning("背景故事文件为空，跳过总结")
        return False

    story_mtime = story_path.stat().st_mtime

    # 检查缓存是否有效（mtime 未变且非 force）
    if summary_path.exists() and not force:
        try:
            cached = json.loads(summary_path.read_text(encoding="utf-8"))
            if cached.get("source_mtime") == story_mtime:
                logger.info(f"背景故事总结缓存有效（mtime={story_mtime}），跳过总结")
                return False
        except (ValueError, KeyError):
            pass  # 缓存损坏，重新总结

    logger.info(f"开始总结背景故事（原文 {len(story_text)} 字）...")

    prompt = _BACKGROUND_SUMMARY_PROMPT.format(story=story_text)
    messages = [
        {"role": "system", "content": "你是角色背景提取助手。"},
        {"role": "user", "content": prompt},
    ]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm.api_key}",
    }
    payload = {
        "model": llm.model,
        "messages": messages,
    }

    import requests
    try:
        resp = requests.post(llm.api_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"LLM 总结背景故事失败: {e}")
        return False

    if not summary:
        logger.error("LLM 返回空总结")
        return False

    # 写入缓存（含 mtime 用于失效检测）
    cache = {
        "summary": summary,
        "source_mtime": story_mtime,
        "compiled_at": time.time(),
        "story_length": len(story_text),
    }
    summary_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"背景故事总结完成，长度 {len(summary)} 字，已写入 {summary_path}")
    return True


def load_background_summary(state_dir: Path) -> Optional[str]:
    """加载背景故事总结（供 PersonaRenderer 调用）。

    Args:
        state_dir: state 目录 Path

    Returns:
        总结文本，不存在或损坏返回 None
    """
    summary_path = state_dir / "background_summary.json"
    if not summary_path.exists():
        return None
    try:
        cached = json.loads(summary_path.read_text(encoding="utf-8"))
        return cached.get("summary")
    except (ValueError, KeyError):
        return None


def main():
    parser = argparse.ArgumentParser(description="背景故事总结脚本")
    parser.add_argument("--force", action="store_true", help="强制重新总结（忽略 mtime）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    llm = LLMClient(config)

    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    story_path = state_dir / "background_story.md"
    summary_path = state_dir / "background_summary.json"

    if not story_path.exists():
        logger.error(f"背景故事文件不存在: {story_path}")
        logger.error("请先创建 state/background_story.md 并写入故事原文")
        sys.exit(1)

    ok = compile_background(story_path, summary_path, llm, force=args.force)
    if ok:
        logger.info("完成")
    else:
        logger.info("未生成新总结")


if __name__ == "__main__":
    main()
