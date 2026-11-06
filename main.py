"""QQ 群聊/私聊机器人主入口。

流程：
- 群聊被动触发：群消息 -> 入 fast_buffer -> 评分 -> 触发决策（硬因子短冷却立即 / direct 倒计时 / 软因子 / 静默窗口兜底）
- 私聊触发：白名单私聊消息 -> 入 fast_buffer -> direct 回复倒计时（仅冷却，无评分/归因）
- 主动触发：定时器唤醒 -> LLM 调用（无新消息，让 LLM 自主决定要不要主动开口）
- 延迟回复：LLM 输出 reply_delay_minutes -> 消息暂存 -> 下次触发时重新加入 pending

同时发言（P003 v2.4）：
- Bot 的 LLM 调用/发送期间到达的消息，在 ctx 上打标记（_backlog_pending）
- 本轮结束若已进入 direct（或私聊），武装回复倒计时：target 停口 N 秒后才回
- 倒计时期间 target 每有新的发言就重置，最多重置 DIRECT_COUNTDOWN_MAX_RESETS 次
- 目的：把用户连发合并成一次回复，而不是"一条一回"

打断模式（P005）：
- direct target（私聊为对方）在 LLM 调用/发送期间发新内容时置位 _send_interrupt
- 未发出的消息全部停发，存入 _unsent_intent；下一轮 user content 注入"# 被打断的发言"
- 已发出的不撤回；连续完整打断 INTERRUPT_STREAK_LIMIT 次后强制发出一次

最近对话块队列（P006）：
- 每次触发把当前 pending 构造为一个块（消息结构），**LLM 成功后才入队**
- 队列随 conversation.json 持久化（recent_blocks），启动时恢复，不再依赖退出 flush
- API 请求 = system + 已落盘 history + （队列所有块 + 当前 pending）拼接（一条 user）
- 入队后超过 recent_pack_turns 时最老块出队，落盘时才用 render_user_content 重新渲染
- 失败/延迟回复不入队；记忆整理前 flush 进 history（一次原子 save）

并发模型（B2 方案）：
- 接收线程（webhook）只写 fast_buffer（HistoryManager 内 _buffer_lock 保护，持锁极短）
- LLM 工作线程用 _cycle_running 标志保证串行，LLM 调用和发送不持锁
- _cycle_lock 只保护"检查+设置 _cycle_running"（持锁极短），不覆盖 LLM 调用
- 撞 cycle 时注册 ctx._cycle_pending（per-chat），cycle 完成后立即重跑（不等静默窗口）
- 群聊静默窗口：每条新消息重置，150s 无新消息则触发兜底 LLM 调用；私聊无静默窗口

阶段 A 重构：per-group 状态移入 GroupContext（src/group_context.py），
Bot 持有 self.groups: dict[str, GroupContext]，方法通过 ctx 参数访问 per-group 状态。
全局共享单例（napcat/llm/affinity/persona/senders）仍留在 Bot。

阶段 B：多群配置（config.group_ids）+ 全局 cycle 队列。
阶段 I：私聊支持——ChatContext 基类 + PrivateContext，群聊/私聊共用 cycle 与发送链。
"""
import time
import random
import threading
from collections import deque
from datetime import datetime
from typing import Optional

from src.config import load_config
from src.napcat_client import NapCatClient, NapCatWebhookServer
from src.llm_client import LLMClient
from src.affinity import AffinityManager
from src.persona import PersonaRenderer
from src.parser import parse_and_validate, strip_internal_commands
from src.senders.message_sender import NapCatMessageSender
from src.senders.voice_sender import AIRecordVoiceSender, LocalFileVoiceSender, UrlVoiceSender
from src.senders.image_sender import NapCatImageSender
from src.senders.video_sender import NapCatVideoSender
from src.senders.emoji_reactor import EmojiReactor
from src.group_context import GroupContext
from src.private_context import PrivateContext
from src.chat_context import ChatContext
from src.scheduler import FixedTimeScheduler
from src.utils.logger import get_logger

logger = get_logger("main")

# 冷却配置
HARD_COOLDOWN_SECONDS = 3       # 硬因子（@/提问）触发冷却，距上次回复 <5s 推迟
SOFT_COOLDOWN_MIN = 5         # 软因子触发冷却下限
SOFT_COOLDOWN_MAX = 60         # 软因子触发冷却上限
QUIET_WINDOW_SECONDS = 150       # 静默窗口：N 秒无新消息后兜底触发
DIRECT_WINDOW_SECONDS = 90      # 阶段 F：direct 模式活跃窗口，target 在此窗口内发言触发快速触发
DIRECT_REPLY_COUNTDOWN_SECONDS = 9  # P003 v2.4：direct 回复倒计时，target 停口 N 秒后才回
DIRECT_COUNTDOWN_MAX_RESETS = 9      # P003 v2.4：倒计时最多被 target 新发言重置的次数
INTERRUPT_STREAK_LIMIT = 5           # P005：连续完整打断 N 次后，下一次忽略打断强制发出
QUOTED_MESSAGE_MAX_CHARS = 500  # 引用消息原文最大字符数，避免上下文膨胀
PRIVATE_ACTIVE_IDLE_MINUTES = 30   # 私聊主动触发：最近互动 < N 分钟时跳过，避免打断正在聊的天


class Bot:
    """机器人主控制器。"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)

        # 全局共享单例（napcat / llm / affinity / persona / senders）
        # 阶段 D/N：active_scheduler 在 GroupContext / PrivateContext（per-chat 独立倒计时）
        self.napcat = NapCatClient(self.config.napcat.base_url, self.config.napcat.group_ids)
        self.llm = LLMClient(self.config)
        self.affinity = AffinityManager()  # 全局按 QQ（跨群身份一致）
        self.persona_renderer = PersonaRenderer(self.config)

        # Sender 实现（全局共享）
        # 网络资源 sender 注入下载超时与临时目录（从 config.media_download 读取，缺失用默认值）
        md = getattr(self.config, "media_download", None)
        md_timeout = md.timeout if md else 30
        md_video_timeout = md.video_timeout if md else 60
        md_temp_dir = md.temp_dir if md else "media/downloaded"
        # 私聊 AI 语音通过 /get_ai_record 生成，需要一个锚点群号作为角色上下文
        anchor_group_id = self.config.napcat.group_ids[0] if self.config.napcat.group_ids else ""

        self.message_sender = NapCatMessageSender(self.napcat)
        self.ai_voice_sender = AIRecordVoiceSender(
            self.napcat, self.config.voice.ai_record_character, self.config.voice.fallback_to_text,
            anchor_group_id=anchor_group_id, timeout=md_timeout, temp_dir=md_temp_dir,
        )
        self.local_voice_sender = LocalFileVoiceSender(self.napcat)
        self.url_voice_sender = UrlVoiceSender(self.napcat, timeout=md_timeout, temp_dir=md_temp_dir)
        self.image_sender = NapCatImageSender(self.napcat, timeout=md_timeout, temp_dir=md_temp_dir)
        self.video_sender = NapCatVideoSender(self.napcat, timeout=md_video_timeout, temp_dir=md_temp_dir)
        self.emoji_reactor = EmojiReactor(self.napcat)

        # 全局运行时状态
        self.self_qq: str = ""
        self.self_nickname: str = ""
        # LLM cycle 串行控制（B2 方案）：用标志代替 _llm_lock 保证 cycle 串行，
        # LLM 调用和发送不持锁，缩短"撞锁"窗口。
        # _cycle_lock 只保护"检查+设置 _cycle_running"（持锁极短），不覆盖 LLM 调用。
        # _cycle_running: True 表示有 LLM cycle 在跑，新触发撞标志后注册重试
        # _cycle_pending 已移入 GroupContext（per-group，多群下各群独立重试标志）
        self._cycle_lock = threading.Lock()
        self._cycle_running: bool = False
        # 全局 cycle 队列：cycle 结束后处理其他撞 cycle 的聊天（阶段 B，chat_key 泛化）
        self._cycle_queue: deque[str] = deque()
        self._cycle_queue_set: set[str] = set()

        # per-group 上下文表：{group_id -> GroupContext}
        # 阶段 B 起支持多群，启动时为每个配置的 group_id 构造一个
        self.groups: dict[str, GroupContext] = {}
        # per-user 私聊上下文表：{qq -> PrivateContext}，首次收到白名单私聊时懒创建
        self.private_chats: dict[str, PrivateContext] = {}
        # 私聊上下文懒创建锁：接收线程并发到达时避免同一 QQ 创建两个上下文
        self._private_ctx_lock = threading.Lock()
        # 记忆整理调度器：{chat_key -> FixedTimeScheduler}（按每日固定时刻触发）
        self.memory_schedulers: dict[str, FixedTimeScheduler] = {}

    def warmup(self):
        """启动预热。"""
        self.napcat.warmup()
        self.self_qq = str(self.napcat.self_info.get("user_id", ""))
        self.self_nickname = self.napcat.self_info.get("nickname", "")

        # 为每个配置的 group_id 创建 GroupContext
        for group_id in self.config.napcat.group_ids:
            ctx = GroupContext(
                group_id=group_id,
                config=self.config,
                napcat=self.napcat,
                affinity=self.affinity,
                self_qq=self.self_qq,
                persona_name=self.config.persona.name,
            )
            self.groups[group_id] = ctx
            self._start_memory_scheduler(ctx)

        if self._memory_enabled():
            cfg = self.config.memory
            logger.info(
                f"记忆系统已启用：{len(self.memory_schedulers)} 个 chat 已启动整理调度，"
                f"时刻 {cfg.consolidate_times}，日记保留 {cfg.recent_journal_hours}h/"
                f"{cfg.recent_journal_max_entries} 条，备忘 TTL {cfg.memo_default_ttl_hours}h"
            )
        else:
            logger.info("记忆系统已禁用")

        # 探测 AI 语音 character 是否可用（不阻塞启动）
        self._probe_ai_voice_character()
        logger.info(f"预热完成，机器人 {self.self_nickname}({self.self_qq})，"
                    f"群 {list(self.groups.keys())}")
        if self.config.private and self.config.private.enabled:
            allow = self.config.private.allow_list
            if allow:
                logger.info(f"私聊已启用，白名单 {len(allow)} 人：{allow}")
            else:
                logger.warning("私聊已启用但 allow_list 为空，所有私聊消息都会被忽略")

            # 私聊主动触发：为白名单用户预建上下文并启动 per-user 调度器。
            # 预建可让重启后仍按持久化的 next_active_check_time 继续主动触发；
            # 未在白名单的用户不会创建。先用好友列表补昵称，避免主动消息里显示 QQ 号。
            if self._private_active_trigger_enabled():
                friend_names = {}
                try:
                    for f in self.napcat.get_friend_list():
                        fqq = str(f.get("user_id", ""))
                        if fqq:
                            friend_names[fqq] = f.get("remark") or f.get("nickname") or fqq
                except Exception as e:
                    logger.warning(f"获取好友列表失败（私聊主动触发昵称回退 QQ）: {e}")

                for qq in sorted({str(q) for q in allow}):
                    if qq == self.self_qq:
                        continue
                    self._get_or_create_private_ctx(qq, friend_names.get(qq, qq))
                pa = self.config.private.active_trigger
                logger.info(f"私聊主动触发调度器已启动：{len(self.private_chats)} 个对象，"
                            f"间隔 {pa.min_interval_minutes}-{pa.max_interval_minutes} 分钟随机，"
                            f"深夜 {pa.night_start_hour}:00-{pa.night_end_hour}:00 禁用")
            else:
                logger.info("私聊主动触发调度器已禁用")
        else:
            logger.info("私聊已禁用")

        # 阶段 D：为每群启动主动触发调度器（per-group 独立倒计时）
        if self.config.active_trigger and self.config.active_trigger.enabled:
            for gid, ctx in self.groups.items():
                ctx.active_scheduler.start(self.on_active_trigger, gid)
            logger.info(f"主动触发调度器已启动：{len(self.groups)} 个群，"
                        f"间隔 {self.config.active_trigger.min_interval_minutes}-"
                        f"{self.config.active_trigger.max_interval_minutes} 分钟随机，"
                        f"深夜 {self.config.active_trigger.night_start_hour}:00-"
                        f"{self.config.active_trigger.night_end_hour}:00 禁用")
        else:
            logger.info("主动触发调度器已禁用")

    def _probe_ai_voice_character(self):
        """探测配置的 AI 语音 character 是否在可用列表中。

        失败不阻塞启动，仅记 WARNING（语音调用时会触发 fallback_to_text）。
        """
        character = self.config.voice.ai_record_character
        if not self.config.napcat.group_ids:
            logger.warning("未配置群号，跳过 AI 语音 character 探测")
            return
        group_id = self.config.napcat.group_ids[0]  # 用第一个群探测
        try:
            characters = self.napcat.get_ai_characters(group_id)
            if not characters:
                logger.warning("无法获取 AI 语音角色列表（可能 NapCat 版本不支持），跳过 character 探测")
                return
            ids = [c.get("character_id", "") for c in characters]
            if character in ids:
                logger.info(f"AI 语音 character '{character}' 探测可用")
            else:
                names = [c.get("character_name", "") for c in characters]
                logger.warning(
                    f"AI 语音 character '{character}' 不在可用列表中！"
                    f"可用角色: {list(zip(ids, names))}。语音调用将触发 fallback_to_text 降级。"
                )
        except Exception as e:
            logger.warning(f"AI 语音 character 探测失败: {e}（不阻塞启动）")

    def on_group_recall(self, data: dict):
        """处理群撤回通知（接收线程，异步调用）。

        NapCat 上报 group_recall notice，data 含 message_id（被撤回的消息）、
        operator_id（撤回操作者 QQ）、group_id。追加一条撤回通知伪消息到 fast_buffer，
        下一轮 drain 时进入 pending，LLM 可据此理解"哪条消息被撤回了"。
        """
        try:
            recalled_msg_id = str(data.get("message_id", ""))
            operator_id = str(data.get("operator_id", ""))
            group_id = str(data.get("group_id", ""))
            if not recalled_msg_id:
                logger.warning(f"撤回通知缺少 message_id: {data}")
                return

            ctx = self.groups.get(group_id)
            if ctx is None:
                return

            logger.info(f"收到撤回通知: recalled_msg_id={recalled_msg_id} operator={operator_id}")
            ctx.history.append_recall_notice(recalled_msg_id, operator_id)
        except Exception as e:
            logger.error(f"处理撤回通知失败: {e}", exc_info=True)

    def on_group_poke(self, data: dict):
        """处理群戳一戳通知（接收线程，异步调用）。

        NapCat 上报 notify/poke notice，data 含 target_id（被戳者）、user_id（戳人者）、
        group_id。只处理 target_id == 自己 QQ 的戳一戳（别人被戳忽略）。
        追加伪消息到 fast_buffer 后硬触发（像被 @ 一样立即反应）。
        """
        try:
            target_id = str(data.get("target_id", ""))
            poker_id = str(data.get("user_id", ""))
            group_id = str(data.get("group_id", ""))
            # 只处理戳自己（别人被戳不处理，避免噪音）
            if target_id != self.self_qq:
                logger.debug(f"忽略非戳自己的 poke: target={target_id} poker={poker_id}")
                return

            ctx = self.groups.get(group_id)
            if ctx is None:
                return

            poker_nick = self.napcat.get_nickname(group_id, poker_id)
            logger.info(f"收到戳一戳: poker={poker_nick}({poker_id})")
            # 追加伪消息到 fast_buffer
            ctx.history.append_poke_notice(poker_id, poker_nick)
            # 硬触发：像被 @ 一样立即反应
            self._try_trigger_immediate(ctx, hard=True)
            # 重置静默定时器
            self._reschedule_quiet_trigger(ctx)
        except Exception as e:
            logger.error(f"处理戳一戳失败: {e}", exc_info=True)

    # ---------- 私聊 ----------
    def _is_private_allowed(self, user_qq: str) -> bool:
        """私聊准入：启用 + 非自己 + 白名单。"""
        if not self.config.private or not self.config.private.enabled:
            return False
        if not user_qq or user_qq == self.self_qq:
            return False
        if user_qq not in set(self.config.private.allow_list):
            logger.debug(f"私聊 {user_qq} 不在白名单，忽略")
            return False
        return True

    def _get_or_create_private_ctx(self, user_qq: str, nickname: str = "") -> PrivateContext:
        """获取或懒创建私聊上下文（加锁避免并发创建重复上下文）。"""
        with self._private_ctx_lock:
            ctx = self.private_chats.get(user_qq)
            if ctx is None:
                ctx = PrivateContext(user_qq, nickname or user_qq, self.config)
                self.private_chats[user_qq] = ctx
                logger.info(f"创建私聊上下文：{ctx.peer_nickname}({user_qq})，状态目录 state/private/{user_qq}/")
                # 记忆整理调度器与主动触发无关，私聊开启记忆时都要启动
                self._start_memory_scheduler(ctx)
                if self._private_active_trigger_enabled():
                    try:
                        ctx.active_scheduler.start(self.on_private_active_trigger, user_qq)
                        logger.info(f"私聊 {user_qq} 主动触发调度器已启动")
                    except Exception as e:
                        # 调度器启动失败不阻塞私聊消息处理
                        logger.error(f"私聊 {user_qq} 主动触发调度器启动失败: {e}", exc_info=True)
            elif nickname and nickname != user_qq and nickname != ctx.peer_nickname:
                ctx.peer_nickname = nickname
            return ctx

    def _private_active_trigger_enabled(self) -> bool:
        """私聊主动触发是否启用。"""
        private_cfg = getattr(self.config, "private", None)
        active_cfg = getattr(private_cfg, "active_trigger", None) if private_cfg else None
        return bool(private_cfg and private_cfg.enabled and active_cfg and active_cfg.enabled)

    # ---------- 记忆系统 ----------
    def _memory_enabled(self) -> bool:
        cfg = getattr(self.config, "memory", None)
        return bool(cfg and cfg.enabled)

    def _memory_memo_enabled(self) -> bool:
        cfg = getattr(self.config, "memory", None)
        return bool(cfg and cfg.enabled and cfg.memo_enabled)

    def _start_memory_scheduler(self, ctx: ChatContext):
        """为某个 chat 启动每日固定时刻的记忆整理调度器。"""
        if not self._memory_enabled():
            return
        if ctx.chat_key in self.memory_schedulers:
            return
        times = getattr(self.config.memory, "consolidate_times", None) or []
        if not times:
            logger.info(f"{ctx.chat_key} 未配置记忆整理时刻，跳过调度")
            return
        scheduler = FixedTimeScheduler(
            times,
            lambda ctx=ctx: self.on_memory_consolidation(ctx),
            label=f"记忆整理 {ctx.chat_key}",
        )
        self.memory_schedulers[ctx.chat_key] = scheduler
        scheduler.start()

    def _should_compact_memory(self, ctx: ChatContext) -> bool:
        """历史轮数达到上限时，本轮要求模型先整理记忆。"""
        if not self._memory_enabled():
            return False
        limit_turns = max(1, self.config.trigger.history_limit // 2)
        return ctx.history.turn_count() >= limit_turns

    def _has_memory_to_consolidate(self, ctx: ChatContext) -> bool:
        """是否还有值得整理的历史/记忆。"""
        return (
            ctx.history.turn_count() > 0
            or bool(ctx.history.recent_blocks)
            or ctx.memory.has_journal()
            or bool(ctx.memory.get_core())
        )

    def _apply_memory_updates(self, ctx: ChatContext, parsed, allow_core: bool) -> bool:
        """把模型输出的 journal/core/memo 指令写入 MemoryManager。

        Args:
            allow_core: 只有记忆整理（阈值触发或定时整理）时才允许替换长期记忆，
                        避免普通轮次频繁更新 core 导致 system prompt 缓存频繁失效。

        Returns:
            True 表示本轮产出了可用的记忆（core 或 journal），可用于决定是否裁剪原文。
        """
        if not self._memory_enabled():
            return False

        wrote_memory = False
        # 日记先写入，再按 core 更新后的保留策略淘汰旧条目
        for text in parsed.journal:
            if ctx.memory.add_journal(text):
                wrote_memory = True
        if allow_core and parsed.core_update:
            if ctx.memory.set_core(parsed.core_update):
                logger.info(f"{ctx.chat_key} 长期记忆已更新（{len(parsed.core_update)} 字）")
            ctx.memory.prune_journal()
            wrote_memory = True

        if self._memory_memo_enabled():
            for text in parsed.memos:
                ctx.memory.add_memo(text)
            if parsed.forget_ids:
                ctx.memory.remove_memos(parsed.forget_ids)

        return wrote_memory

    def on_memory_consolidation(self, ctx: ChatContext):
        """定时记忆整理回调（每日固定时刻，由 FixedTimeScheduler 触发）。

        整理 cycle 不回复聊天、不消费 pending；只读取 history 并更新 core/journal。
        撞其他 chat 的 cycle 时跳过本次，避免把正常对话拖慢。
        """
        try:
            if not self._memory_enabled():
                return
            if not self._has_memory_to_consolidate(ctx):
                logger.info(f"{ctx.chat_key} 记忆整理跳过：没有可整理的历史")
                return
            if not self._try_enter_cycle(ctx, register_retry=False):
                logger.info(f"{ctx.chat_key} 记忆整理撞 cycle，跳过本次")
                return
            try:
                # P006：整理前先把最近对话队列落盘，让整理能看到最近几轮
                self._flush_recent_blocks(ctx)
                self._run_memory_cycle(ctx)
            finally:
                self._exit_cycle(ctx)
                self._drain_cycle_queue()
        except Exception as e:
            logger.error(f"{ctx.chat_key} 记忆整理异常: {e}", exc_info=True)

    def _run_memory_cycle(self, ctx: ChatContext):
        """执行一次记忆整理（不发送聊天消息，不写 conversation turn）。"""
        core = ctx.memory.get_core()
        journal = ctx.memory.render_journal_for_consolidation()
        memo = ctx.memory.render_memo_block()
        system_prompt = self.persona_renderer.render_system_prompt(
            core_memory=core, chat_type=ctx.chat_type
        )
        history_messages = ctx.history.get_messages_for_llm()
        user_content = self.persona_renderer.render_memory_consolidation(
            core, journal, memo
        )
        logger.info(f"{ctx.chat_key} 记忆整理开始（历史 {ctx.history.turn_count()} 轮）")
        raw_result = self.llm.chat(system_prompt, history_messages, user_content)
        if raw_result is None:
            logger.warning(f"{ctx.chat_key} 记忆整理 LLM 调用失败，保留原文，下次再试")
            return

        parsed = parse_and_validate(raw_result)
        wrote_memory = self._apply_memory_updates(ctx, parsed, allow_core=True)
        if wrote_memory:
            keep_turns = max(1, self.config.trigger.history_keep_recent // 2)
            ctx.history.trim_to_recent(keep_turns)
            logger.info(f"{ctx.chat_key} 记忆整理完成")
        else:
            logger.warning(f"{ctx.chat_key} 记忆整理未产出 core/journal，保留原文，下次再试")

    def on_private_message(self, msg: dict):
        """处理收到的私聊消息（接收线程）。

        私聊是群聊的减法：不评分、不归因、不设静默窗口。
        对方每发一条消息就武装/重置 direct 回复倒计时（停口 5s 后才回，连发合并）；
        若 Bot 正在 LLM 调用/发送，新消息进 fast_buffer 并打 _backlog_pending 标记，
        本轮结束后由倒计时合并补看一眼。
        """
        try:
            user_qq = str(msg.get("user_id", ""))
            if not self._is_private_allowed(user_qq):
                return

            sender = msg.get("sender") or {}
            sender_nick = sender.get("nickname") or msg.get("nickname") or user_qq
            content, images, has_voice = self._extract_incoming_content(msg)
            msg_id = str(msg.get("message_id", ""))

            if has_voice:
                content = (content + (" " if content else "") + "[语音消息]").strip()

            ctx = self._get_or_create_private_ctx(user_qq, sender_nick)

            # 入 fast_buffer（与 bot 回复共用入口，保持真实发言顺序）
            ctx.history.append_group_message(user_qq, sender_nick, content, msg_id, images=images)
            # P003 v2.4 / P005：私聊 cycle 正在跑 -> 打标记并打断尚未发出的消息
            if ctx._cycle_active:
                ctx._backlog_pending = True
                ctx._send_interrupt.set()
                ctx._interrupt_sender = sender_nick
                logger.debug(f"{ctx.chat_key} 输出期间收到新消息，置位打断信号")

            # 语音转写：异步延迟重试，成功后回填历史 content
            if has_voice and msg_id:
                threading.Thread(target=self._transcribe_voice_async, args=(ctx, msg_id), daemon=True).start()

            # 私聊是 1v1：所有对方发言都走 direct 回复倒计时（停口后再回，合并连发）
            self._maybe_arm_direct_countdown(ctx, new_speech=True)
        except Exception as e:
            logger.error(f"处理私聊消息异常: {e}", exc_info=True)

    def on_private_recall(self, data: dict):
        """处理私聊撤回通知（friend_recall）。

        user_id 为撤回者（私聊对象）。追加撤回通知伪消息后触发一次 LLM 看一眼。
        """
        try:
            user_qq = str(data.get("user_id", "") or data.get("operator_id", ""))
            if not self._is_private_allowed(user_qq):
                return
            recalled_msg_id = str(data.get("message_id", ""))
            if not recalled_msg_id:
                logger.warning(f"私聊撤回通知缺少 message_id: {data}")
                return
            ctx = self.private_chats.get(user_qq)
            if ctx is None:
                return
            logger.info(f"收到私聊撤回通知: recalled_msg_id={recalled_msg_id} user={user_qq}")
            ctx.history.append_recall_notice(recalled_msg_id, user_qq)
            self._try_trigger_private(ctx)
        except Exception as e:
            logger.error(f"处理私聊撤回通知失败: {e}", exc_info=True)

    def on_private_poke(self, data: dict):
        """处理私聊戳一戳通知。

        只处理 target_id == 自己 QQ 的戳一戳。追加伪消息后立即尝试触发。
        """
        try:
            target_id = str(data.get("target_id", ""))
            poker_id = str(data.get("user_id", ""))
            if target_id != self.self_qq:
                logger.debug(f"忽略非戳自己的私聊 poke: target={target_id} poker={poker_id}")
                return
            if not self._is_private_allowed(poker_id):
                return
            ctx = self.private_chats.get(poker_id) or self._get_or_create_private_ctx(poker_id)
            logger.info(f"收到私聊戳一戳: poker={ctx.peer_nickname}({poker_id})")
            ctx.history.append_poke_notice(poker_id, ctx.peer_nickname)
            self._try_trigger_private(ctx)
        except Exception as e:
            logger.error(f"处理私聊戳一戳失败: {e}", exc_info=True)

    def on_group_message(self, msg: dict):
        """处理收到的群消息（接收线程，无 _cycle_lock）。

        职责：
        1. 路由到对应 GroupContext
        2. 入 fast_buffer（HistoryManager 内 _buffer_lock 保护，持锁极短）
        3. 评分（只读配置/亲密度，无锁）
        4. 触发决策：
           - 硬因子（@/提问）且距上次回复 ≥ HARD_COOLDOWN_SECONDS → 立即尝试触发
           - 其他 → 只入 buffer，由静默窗口兜底
        5. 无论如何重置静默定时器（每条新消息都推迟兜底触发）
        """
        try:
            msg_group_id = str(msg.get("group_id", ""))

            ctx = self.groups.get(msg_group_id)
            if ctx is None:
                logger.debug(f"未找到群 {msg_group_id} 的上下文，丢弃消息")
                return

            sender_qq = str(msg.get("user_id", ""))
            sender_nick = self.napcat.get_nickname(msg_group_id, sender_qq)
            content, images, has_voice = self._extract_incoming_content(msg)
            msg_id = str(msg.get("message_id", ""))

            # 语音消息：先用占位入历史（不阻塞），独立线程延迟重试转写后回填
            # NapCat 收到语音需先从腾讯下载 amr 文件，立即调用 fetch_ptt_text 会因文件未就绪而失败
            if has_voice:
                content = (content + (" " if content else "") + "[语音消息]").strip()

            # 1. 入 fast_buffer（无 _cycle_lock，仅 HistoryManager 内 _buffer_lock）
            ctx.history.append_group_message(sender_qq, sender_nick, content, msg_id, images=images)
            # P003 v2.4：本群 cycle 正在跑 -> 这条消息是"Bot 输出期间到达的欠账"，先打标记
            if ctx._cycle_active:
                ctx._backlog_pending = True
                # P005 打断：direct target 在本轮输出期间发新内容 -> 打断尚未发出的消息
                deadline = ctx.direct_targets.get(sender_qq)
                if deadline is not None and deadline > time.time():
                    ctx._send_interrupt.set()
                    ctx._interrupt_sender = sender_nick
                    logger.debug(f"{ctx.chat_key} 输出期间收到新消息，置位打断信号")

            # 语音转写：异步延迟重试，成功后回填历史 content
            if has_voice and msg_id:
                threading.Thread(target=self._transcribe_voice_async, args=(ctx, msg_id), daemon=True).start()

            # 2. 评分（接收线程，只读）
            score, soft_factors = ctx.trigger_evaluator.evaluate(msg)
            is_hard = self._is_hard_trigger(ctx, msg, content)
            logger.debug(f"消息评分={score} hard={is_hard} soft_factors={[f.name for f in soft_factors]}")

            # 3. 触发决策（按优先级：硬因子 > direct 倒计时 > 软因子 > 静默兜底）
            if is_hard:
                self._try_trigger_immediate(ctx, hard=True)
            else:
                # 阶段 F：direct 目标的发言。P003 v2.4 起不再立即触发，改为武装/重置回复倒计时，
                # 等 target 停口后再回（避免用户连发时一条一回）
                now_ts = time.time()
                direct_deadline = ctx.direct_targets.get(sender_qq)
                if direct_deadline is not None and direct_deadline > now_ts:
                    # 续命：重置该 target 的 deadline
                    ctx.direct_targets[sender_qq] = now_ts + DIRECT_WINDOW_SECONDS
                    self._maybe_arm_direct_countdown(ctx, new_speech=True)
                elif ctx.trigger_evaluator.should_peek(score):
                    self._try_trigger_immediate(ctx, hard=False, soft_factors=soft_factors)
                # 低分消息不立即触发，等静默窗口兜底

            # 4. 重置静默定时器（无论硬软，新消息都推迟兜底触发）
            self._reschedule_quiet_trigger(ctx)

        except Exception as e:
            logger.error(f"处理消息异常: {e}", exc_info=True)

    def _extract_incoming_content(self, msg: dict) -> tuple[str, list[dict], bool]:
        """提取消息文本、图片源与语音标记。

        含图片或引用时从消息段重建文本：
        - 图片替换为 [图片] 标记，同时收集 image 段 data 供本轮识图使用
        - 引用段调用 /get_msg 补全被引用消息的原文与图片，嵌入 [引用 昵称(QQ): 原文]
        不含图片也不含引用时沿用 raw_message，保持原有行为。

        Returns:
            (content, images, has_voice)
        """
        segments = msg.get("message") or []
        has_image = any(isinstance(seg, dict) and seg.get("type") == "image" for seg in segments)
        has_reply = any(isinstance(seg, dict) and seg.get("type") == "reply" for seg in segments)
        has_voice = any(isinstance(seg, dict) and seg.get("type") == "record" for seg in segments)

        if not has_image and not has_reply:
            content = msg.get("raw_message", "") or _extract_text_from_msg(msg)
            return content, [], has_voice

        parts = []
        images = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type", "")
            data = seg.get("data", {}) or {}
            if t == "text":
                parts.append(data.get("text", ""))
            elif t == "image":
                parts.append("[图片]")
                images.append(data)
            elif t == "record":
                # 语音占位由调用方统一追加，避免重复
                continue
            elif t == "at":
                parts.append(f"@{data.get('qq', '')}")
            elif t == "face":
                parts.append("[表情]")
            elif t == "reply":
                quoted = self._fetch_quoted_message(data)
                if quoted:
                    qtext, qimages = self._format_quoted_message(quoted)
                    parts.append(f"[{qtext}]" if qtext else "[引用]")
                    images.extend(qimages)
                else:
                    parts.append("[引用]")
            elif t == "forward":
                parts.append("[合并转发]")
            elif t == "file":
                parts.append("[文件]")
            else:
                parts.append(f"[{t}]")

        content = "".join(parts).strip()
        if not content:
            content = "[图片]" if images else (msg.get("raw_message", "") or "")
        return content, images, has_voice

    def _fetch_quoted_message(self, reply_data: dict) -> dict:
        """通过 /get_msg 获取被引用消息详情。失败/过期返回空 dict。"""
        msg_id = str(
            reply_data.get("id")
            or reply_data.get("message_id")
            or reply_data.get("target_msg_id")
            or ""
        )
        if not msg_id:
            return {}
        quoted = self.napcat.get_msg(msg_id, quiet=True)
        if quoted:
            logger.debug(f"引用消息补全成功: quoted_msg_id={msg_id}")
        else:
            logger.info(f"引用消息补全失败/已过期: quoted_msg_id={msg_id}")
        return quoted

    def _format_quoted_message(self, quoted: dict) -> tuple[str, list[dict]]:
        """把被引用消息格式化为一行文本，并返回其中的图片源。"""
        sender = quoted.get("sender") or {}
        nick = (
            sender.get("card")
            or sender.get("nickname")
            or quoted.get("nickname")
            or str(quoted.get("user_id", ""))
            or "某人"
        )
        qq = str(sender.get("user_id") or quoted.get("user_id") or "")

        raw = quoted.get("message")
        if isinstance(raw, dict):
            raw = [raw]
        images = []
        voice_text = None
        if isinstance(raw, list):
            parts = []
            for seg in raw:
                if not isinstance(seg, dict):
                    continue
                t = seg.get("type", "")
                data = seg.get("data", {}) or {}
                if t == "text":
                    parts.append(data.get("text", ""))
                elif t == "image":
                    parts.append("[图片]")
                    images.append(data)
                elif t == "at":
                    parts.append(f"@{data.get('qq', '')}")
                elif t == "face":
                    parts.append("[表情]")
                elif t == "reply":
                    parts.append("[引用]")
                elif t == "forward":
                    parts.append("[合并转发]")
                elif t == "record":
                    if voice_text is None:
                        voice_text = self._fetch_quoted_voice_text(quoted)
                    parts.append(f"[语音] {voice_text}" if voice_text else "[语音]")
                elif t == "file":
                    parts.append("[文件]")
                else:
                    parts.append(f"[{t}]")
            text = "".join(parts)
        else:
            text = str(raw or quoted.get("raw_message", ""))

        # 压缩换行/多余空白，截断避免上下文膨胀
        text = " ".join(text.split())
        if len(text) > QUOTED_MESSAGE_MAX_CHARS:
            text = text[:QUOTED_MESSAGE_MAX_CHARS] + "…"
        if not text:
            text = "[图片]" if images else "[空]"

        prefix = f"引用 {nick}({qq}): " if qq else f"引用 {nick}: "
        return prefix + text, images

    def _fetch_quoted_voice_text(self, quoted: dict) -> str:
        """转写被引用消息中的语音（/fetch_ptt_text）。

        被引用语音通常已存在一段时间，文件大概率已就绪；这里做一次立即尝试 +
        一次 1 秒后的轻量重试，查询失败回退为 [语音] 占位，不阻塞太久。
        """
        msg_id = str(quoted.get("message_id") or quoted.get("real_id") or "")
        if not msg_id:
            return ""
        for attempt in range(2):
            if attempt:
                time.sleep(1.0)
            try:
                text = self.napcat.fetch_ptt_text(msg_id, quiet=True, timeout=5)
            except Exception as e:
                logger.debug(f"引用语音转写尝试异常: quoted_msg_id={msg_id} {e}")
                text = ""
            if text:
                logger.info(f"引用语音转写成功: quoted_msg_id={msg_id} text={text[:30]}")
                return text
        logger.info(f"引用语音转写失败/未就绪: quoted_msg_id={msg_id}")
        return ""

    def _transcribe_voice_async(self, ctx: ChatContext, msg_id: str):
        """异步转写语音：分级延迟重试，成功后回填历史 content。

        NapCat 收到语音消息后需先从腾讯下载 amr 文件到本地，立即调用 fetch_ptt_text
        会因文件未就绪而失败（retcode=200）。采用分级延迟重试（1s/1s/2s，共 3 次）：
        每次尝试的失败属正常重试，记 INFO；只有 3 次全部失败才记 ERROR。
        成功则把历史中的 [语音消息] 占位回填为 [语音] 转写文字。
        """
        delays = [1.0, 1.0, 2.0]  # 分级延迟：快探测→中等→慢兜底，总最坏 4s
        total_attempts = len(delays)
        for attempt, delay in enumerate(delays, 1):
            time.sleep(delay)
            try:
                # quiet=True：重试期间的 _call 业务失败记 INFO，避免刷 ERROR
                text = self.napcat.fetch_ptt_text(msg_id, quiet=True)
                if text:
                    new_content = f"[语音] {text}"
                    updated = ctx.history.update_group_message_content(msg_id, new_content)
                    if updated:
                        logger.info(f"语音转文字成功并回填: {text[:50]}（第{attempt}/{total_attempts}次尝试）")
                    else:
                        logger.info(f"语音转文字成功但消息已被消费（msg_id={msg_id}）: {text[:50]}")
                    return
                logger.info(f"语音转文字第{attempt}/{total_attempts}次未就绪（msg_id={msg_id}）")
            except Exception as e:
                logger.info(f"语音转文字第{attempt}/{total_attempts}次异常（msg_id={msg_id}）: {e}")
        logger.error(f"语音转文字 {total_attempts} 次重试均失败，保留占位（msg_id={msg_id}）")

    def _is_hard_trigger(self, ctx: GroupContext, msg: dict, content: str) -> bool:
        """判断是否硬因子触发（@bot 或提问语气）。"""
        if ctx.trigger_evaluator._check_at_me(msg):
            return True
        if ctx.trigger_evaluator._check_question_to_me(content):
            return True
        return False

    def _try_enter_cycle(self, ctx: ChatContext, register_retry: bool = True) -> bool:
        """尝试进入 LLM cycle（B2 方案）。

        用 _cycle_running 标志保证 cycle 串行，代替 _llm_lock 覆盖全流程。
        _cycle_lock 只保护"检查+设置"（持锁极短），LLM 调用和发送不持锁。

        Args:
            ctx: 群上下文。撞 cycle 时把 _cycle_pending 标记到该 ctx（per-group）。
            register_retry: False=撞 cycle 时不注册重试（记忆整理用，跳过本次即可）。

        Returns:
            True: 成功进入 cycle（调用方负责执行 _run_llm_cycle + _exit_cycle）
            False: 已有 cycle 在跑（register_retry=True 时已注册重试）
        """
        with self._cycle_lock:
            if self._cycle_running:
                if register_retry:
                    ctx._cycle_pending = True
                    self._enqueue_chat(ctx.chat_key)
                return False
            self._cycle_running = True
            ctx._cycle_active = True
            return True

    def _enqueue_chat(self, chat_key: str):
        """聊天入队 cycle 队列（去重）。在 _cycle_lock 保护下调用。"""
        if chat_key not in self._cycle_queue_set:
            self._cycle_queue.append(chat_key)
            self._cycle_queue_set.add(chat_key)

    def _get_context(self, chat_key: str) -> Optional[ChatContext]:
        """按 chat_key 解析上下文：group:{gid} / private:{qq}。"""
        kind, _, chat_id = chat_key.partition(":")
        if kind == "group":
            return self.groups.get(chat_id)
        if kind == "private":
            return self.private_chats.get(chat_id)
        return None

    def _exit_cycle(self, ctx: ChatContext):
        """退出 LLM cycle，清除运行标志与 ctx 的活跃标记。"""
        with self._cycle_lock:
            ctx._cycle_active = False
            self._cycle_running = False

    def _drain_cycle_queue(self):
        """处理 cycle 队列：cycle 结束后处理其他撞 cycle 的群。

        迭代处理（非递归），避免多群排队时递归过深。
        每次从队首取一个群，执行 _run_llm_cycle，结束后继续取下一个。
        """
        while self._cycle_queue:
            chat_key = self._cycle_queue.popleft()
            self._cycle_queue_set.discard(chat_key)
            ctx = self._get_context(chat_key)
            if ctx is None or not ctx._cycle_pending:
                continue  # 已被消费或不存在

            with self._cycle_lock:
                if self._cycle_running:
                    # 不应该发生（_cycle_running 刚清），防御性重新入队
                    self._cycle_queue.appendleft(chat_key)
                    self._cycle_queue_set.add(chat_key)
                    return
                self._cycle_running = True
                ctx._cycle_active = True

            try:
                logger.debug(f"从队列处理 {chat_key} 的 pending cycle")
                self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
            finally:
                self._exit_cycle(ctx)

    def _consume_pending_trigger(self, ctx: ChatContext) -> bool:
        """检查并消费重试标志（_run_llm_cycle while 循环末尾调用）。

        Returns:
            True: 有触发在 cycle 期间撞上，需要重跑
            False: 无重试，退出循环
        """
        with self._cycle_lock:
            if ctx._cycle_pending:
                ctx._cycle_pending = False
                return True
            return False

    def _try_trigger_immediate(self, ctx: ChatContext, hard: bool = False, soft_factors=None,
                               direct: bool = False):
        """立即尝试触发 LLM（硬因子 / direct 倒计时 / 软因子路径）。

        冷却检查：距上次回复 < cooldown 则跳过（改为武装回复倒计时）。
        cycle 串行检查：撞 _cycle_running 则注册重试，cycle 完成后立即重跑。

        Args:
            ctx: 聊天上下文（群聊/私聊）
            hard: True=硬因子触发（@/提问），False=非硬因子（群聊专用）
            soft_factors: 软因子触发时传入命中的软因子列表，供归因使用；
                         硬因子/direct 触发传 None，归因跳过。
            direct: True=direct 倒计时到期触发（阶段 F，绕过 peek_threshold，归因跳过）
        """
        # 私聊走独立触发路径：无评分/归因/direct，只做冷却 + cycle 串行
        if ctx.chat_type == "private":
            self._try_trigger_private(ctx)
            return

        now = time.time()
        elapsed = now - ctx.last_reply_time
        if direct:
            cooldown = SOFT_COOLDOWN_MIN
            trigger_label = "direct倒计时"
        elif hard:
            cooldown = HARD_COOLDOWN_SECONDS
            trigger_label = "硬因子"
        else:
            cooldown = SOFT_COOLDOWN_MIN
            trigger_label = "软因子"
        if elapsed < cooldown:
            logger.debug(f"{trigger_label}触发但冷却中（elapsed={elapsed:.1f}s < {cooldown}s），"
                         f"重新武装 direct 倒计时")
            # 撞冷却不丢弃：若仍在 direct 且有未看过的消息，重新武装倒计时
            self._maybe_arm_direct_countdown(ctx, new_speech=False)
            return

        if not self._try_enter_cycle(ctx):
            logger.debug(f"{trigger_label}触发但 cycle 在跑，已入队等待")
            return

        try:
            self._run_llm_cycle(ctx, soft_factors=soft_factors, is_active=False)
        finally:
            self._exit_cycle(ctx)
            self._drain_cycle_queue()

    def _cooldown_seconds(self, ctx: ChatContext) -> int:
        """当前聊天的冷却秒数：群聊用 SOFT_COOLDOWN_MIN，私聊用配置值。"""
        if ctx.chat_type == "private":
            cfg = self.config.private
            return cfg.cooldown_seconds if cfg else 3
        return SOFT_COOLDOWN_MIN

    def _try_trigger_private(self, ctx: PrivateContext):
        """私聊触发：冷却结束后进入 cycle；撞 cycle 则入队等待。

        不做评分/归因/direct 判断。正常消息由 direct 回复倒计时到期后调用；
        撤回/戳一戳等事件走此路径立即触发。冷却期内到达的消息重新武装倒计时。
        """
        now = time.time()
        cooldown = self._cooldown_seconds(ctx)
        elapsed = now - ctx.last_reply_time
        if elapsed < cooldown:
            logger.debug(f"私聊触发但冷却中（elapsed={elapsed:.1f}s < {cooldown}s），重新武装回复倒计时")
            self._maybe_arm_direct_countdown(ctx, new_speech=False)
            return

        if not self._try_enter_cycle(ctx):
            logger.debug("私聊触发但 cycle 在跑，已入队等待")
            return

        try:
            self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
        finally:
            self._exit_cycle(ctx)
            self._drain_cycle_queue()

    def _schedule_private_delayed_trigger(self, ctx: PrivateContext, minutes: int):
        """私聊延迟回复到期唤醒：N 分钟后重新触发一次 LLM。"""
        delay = max(1.0, minutes * 60) + 1.0  # 多等 1s，确保 due_time 已过
        with ctx._delayed_timer_lock:
            if ctx._delayed_timer is not None:
                ctx._delayed_timer.cancel()
            timer = threading.Timer(delay, self._on_private_delayed_timeout, args=(ctx,))
            timer.daemon = True
            timer.start()
            ctx._delayed_timer = timer
        logger.info(f"私聊延迟回复排程：{minutes} 分钟后唤醒（{ctx.chat_id}）")

    def _on_private_delayed_timeout(self, ctx: PrivateContext):
        """私聊延迟回复定时器到期：重新触发 LLM。"""
        try:
            with ctx._delayed_timer_lock:
                ctx._delayed_timer = None
            logger.info(f"私聊延迟回复到期，重新触发（{ctx.chat_id}）")
            self._try_trigger_private(ctx)
        except Exception as e:
            logger.error(f"私聊延迟回复唤醒异常: {e}", exc_info=True)

    def _reschedule_quiet_trigger(self, ctx: GroupContext, delay: float = QUIET_WINDOW_SECONDS):
        """重置静默窗口定时器：N 秒后若仍无新消息则兜底触发。

        每条新消息都调用此方法，cancel 旧定时器并重设。
        """
        with ctx._quiet_timer_lock:
            if ctx._quiet_timer is not None:
                ctx._quiet_timer.cancel()
            timer = threading.Timer(delay, self._on_quiet_timeout, args=(ctx,))
            timer.daemon = True
            timer.start()
            ctx._quiet_timer = timer

    def _on_quiet_timeout(self, ctx: GroupContext):
        """静默窗口到期：兜底触发 LLM。

        冷却检查：距上次回复 < SOFT_COOLDOWN_MIN 则重调度到冷却到期。
        cycle 串行检查：撞 _cycle_running 则注册重试，cycle 完成后立即重跑。
        """
        try:
            now = time.time()
            elapsed = now - ctx.last_reply_time
            if elapsed < SOFT_COOLDOWN_MIN:
                # 还在软冷却内，重调度到冷却到期
                remaining = SOFT_COOLDOWN_MIN - elapsed
                logger.debug(f"静默窗口到期但软冷却中（elapsed={elapsed:.1f}s），{remaining:.1f}s 后再触发")
                self._reschedule_quiet_trigger(ctx, delay=remaining)
                return

            if not self._try_enter_cycle(ctx):
                logger.debug("静默窗口到期但 cycle 在跑，已入队等待")
                return

            try:
                self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
            finally:
                self._exit_cycle(ctx)
                self._drain_cycle_queue()
        except Exception as e:
            logger.error(f"静默窗口兜底触发异常: {e}", exc_info=True)

    def on_active_trigger(self, group_id: str):
        """主动触发回调（由 per-group 调度器调用）。

        阶段 D：每群独立调度器触发，传入对应 group_id。
        """
        try:
            logger.info(f"群 {group_id} 主动触发：执行 LLM 调用")
            ctx = self.groups.get(group_id)
            if ctx is None:
                logger.warning(f"主动触发但未找到群 {group_id} 的上下文")
                return

            if not self._try_enter_cycle(ctx):
                logger.debug(f"群 {group_id} 主动触发但 cycle 在跑，已入队等待")
                return

            try:
                self._run_llm_cycle(ctx, soft_factors=None, is_active=True)
            finally:
                self._exit_cycle(ctx)
                self._drain_cycle_queue()
        except Exception as e:
            logger.error(f"群 {group_id} 主动触发异常: {e}", exc_info=True)

    def on_private_active_trigger(self, peer_qq: str):
        """私聊主动触发回调（由 per-user 调度器调用）。

        与群聊主动触发一致：无 pending 时让 LLM 自主决定是否主动开口。
        额外加一层"不打断正在聊的天"保护：本 chat cycle 在跑、有未看消息、
        或最近 30 分钟内有互动时跳过本次；调度器会自动安排下一次。
        """
        try:
            ctx = self.private_chats.get(peer_qq)
            if ctx is None:
                logger.warning(f"私聊主动触发但未找到 {peer_qq} 的上下文")
                return

            if ctx._cycle_active or ctx.history.has_unseen_buffered_messages():
                logger.info(f"私聊 {peer_qq} 主动触发但会话活跃/有未读消息，跳过本次")
                return
            if time.time() - ctx.last_reply_time < PRIVATE_ACTIVE_IDLE_MINUTES * 60:
                logger.info(f"私聊 {peer_qq} 最近有互动，跳过本次主动触发")
                return

            logger.info(f"私聊 {peer_qq} 主动触发：执行 LLM 调用")
            if not self._try_enter_cycle(ctx):
                logger.debug(f"私聊 {peer_qq} 主动触发但 cycle 在跑，已入队等待")
                return

            try:
                self._run_llm_cycle(ctx, soft_factors=None, is_active=True)
            finally:
                self._exit_cycle(ctx)
                self._drain_cycle_queue()
        except Exception as e:
            logger.error(f"私聊 {peer_qq} 主动触发异常: {e}", exc_info=True)

    def _run_llm_cycle(self, ctx: ChatContext, soft_factors, is_active: bool):
        """LLM 工作循环（B2 方案：调用方已通过 _try_enter_cycle 获取运行权）。

        流程（while 循环，处理撞 cycle 重试）：
        1. drain fast_buffer → pending（_buffer_lock 保护，持锁极短）
        2. 检查延迟回复到期 → 加入 pending
        3. 若 pending 空（且非主动触发）→ 检查重试或退出
        4. 构建 user_content（渲染 [#msg_id] 标记，供 reply/react 段引用）
        5. 调用 LLM（不持锁，_cycle_running 保证串行）
        6. 落地 user/assistant turn（pending 清空，append_turn）
        7. 发送 + 归因（不持锁，发送期间群消息和 bot 回复都进 fast_buffer）
        8. 检查 _cycle_pending：有则继续循环（重跑不传 soft_factors，归因跳过），无则退出

        注：LLM 调用和发送不持任何锁，接收线程写 fast_buffer 不受影响。
        pending/messages/affinity/attribution 只在本线程访问，无竞争。
        """
        while True:
            # 先清掉上一轮遗留的标记，再 drain：
            # 若 drain 期间有新消息到达，接收线程会重新置位，不会在这里被误清掉。
            ctx._backlog_pending = False
            ctx._send_interrupt.clear()
            ctx._interrupt_sender = ""

            # 1. drain fast_buffer
            drained = ctx.history.drain_buffer_to_pending()
            if drained > 0:
                logger.debug(f"drain {drained} 条消息到 pending")

            # 2. 延迟回复到期
            due_count = ctx.history.pop_due_delayed_into_pending()
            if due_count > 0:
                logger.info(f"延迟回复到期，{due_count} 条消息已加入 pending")

            # 3. pending 空检查（主动触发允许空 pending，让 LLM 决定是否主动开口）
            if not ctx.history.pending_group_msgs and not is_active:
                logger.debug("pending 为空，跳过 LLM 调用")
                if self._consume_pending_trigger(ctx):
                    continue
                # 极小概率的竞态：drain 之后、检查之前又有新消息进来
                self._maybe_arm_direct_countdown(ctx, new_speech=False)
                return

            # 4. 构建 user_content（创建快照供 reply/react 段引用）
            #    长期记忆进 system；近期日记与备忘进 user content（缓存安全）
            needs_compact = self._should_compact_memory(ctx)
            core_memory = ctx.memory.get_core() if self._memory_enabled() else ""
            system_prompt = self.persona_renderer.render_system_prompt(
                core_memory=core_memory, chat_type=ctx.chat_type
            )
            # P006：本轮块先构造但**不入队**；请求直接拼接"队列 + 当前 pending"。
            # 只有 LLM 成功后才入队；入队时若超过窗口，最老块才出队落盘 history。
            intent_text = self._format_unsent_intent(ctx)
            block = self._make_recent_block(ctx, is_active, intent_text)

            history_messages = ctx.history.get_messages_for_llm()
            member_list = self._build_member_list(ctx)

            # 当前请求 = system + 已落盘 history +（队列所有块 + 当前 pending）的连续消息记录
            # 复用 HistoryManager.build_user_content 的渲染逻辑，只多传一个"队列前缀"
            pending_text = ctx.history.build_user_content(self._recent_block_entries(ctx))
            journal_block = ""
            memo_block = ""
            if self._memory_enabled():
                # 记忆整理时给全部日记（否则只看近期窗口），确保 core 能吸收完整上下文
                journal_block = (
                    ctx.memory.render_journal_for_consolidation() if needs_compact
                    else ctx.memory.render_journal_block()
                )
                memo_block = ctx.memory.render_memo_block()
            last_state_text = _format_state(ctx._last_state) if ctx._last_state else ""
            render_kwargs = dict(
                is_active=is_active, chat_type=ctx.chat_type, peer=self._private_peer(ctx),
                interrupted_intent=intent_text,
                journal=journal_block, memo=memo_block, memory_consolidation=needs_compact,
                last_state=last_state_text,
            )
            new_user_content = self.persona_renderer.render_user_content(
                pending_text, member_list, self.self_nickname, self.self_qq, **render_kwargs
            )

            # 5. 调用 LLM（不持锁，_cycle_running 标志保证串行）
            #    图片源来自当前 pending 批次；只在本次 user content 中附带，不进历史消息
            pending_images = ctx.history.collect_pending_images()
            if pending_images:
                logger.info(f"{ctx.chat_key} 本轮携带 {len(pending_images)} 张图片进入识图")
            raw_result = self.llm.chat(
                system_prompt, history_messages, new_user_content, images=pending_images
            )
            if raw_result is None:
                logger.warning("LLM 调用失败，本轮跳过")
                # 本轮块未入队，直接丢弃；pending 留待下次触发再拼入（消息不丢失）
                if self._consume_pending_trigger(ctx):
                    continue
                return

            # 6. 解析 + 记忆 + 落地
            parsed = parse_and_validate(raw_result)
            logger.info(f"LLM 返回 action={parsed.action} state={_format_state(parsed.state)}"
                        f"{' reply_delay=' + str(parsed.reply_delay_minutes) + 'min' if parsed.reply_delay_minutes > 0 else ''}"
                        f"{' [主动触发]' if is_active else ''}")
            if parsed.journal or parsed.memos or parsed.forget_ids or (needs_compact and parsed.core_update):
                logger.info(f"{ctx.chat_key} 记忆更新：journal={len(parsed.journal)} "
                            f"core={len(parsed.core_update) if needs_compact else 0} "
                            f"memo={len(parsed.memos)} forget={parsed.forget_ids}")

            # 记忆写入先于延迟回复分支：即使本轮 later，日记/备忘也应落盘
            wrote_memory = self._apply_memory_updates(ctx, parsed, allow_core=needs_compact)

            # 延迟回复处理：LLM 觉得"等会再回"，把这批消息存到 delayed_replies
            if parsed.reply_delay_minutes > 0 and parsed.action == "silent":
                # 本轮块未入队，直接丢弃；pending 转存 delayed
                stashed = ctx.history.stash_pending_as_delayed(parsed.reply_delay_minutes)
                # 私聊没有主动触发兜底，需要自己排程到期唤醒；主动触发时 pending 为空则不排程
                if ctx.chat_type == "private" and stashed > 0:
                    self._schedule_private_delayed_trigger(ctx, parsed.reply_delay_minutes)
                if self._consume_pending_trigger(ctx):
                    continue
                return

            # 完成本轮块：assistant/state 填入块；成功后才入队（出队时才落盘 history）
            stored_assistant = strip_internal_commands(raw_result) or raw_result
            block["assistant"] = stored_assistant
            block["state"] = parsed.state
            block["finalized"] = True
            ctx._last_state = parsed.state
            # 成功才入队；超过窗口时最老块此时才出队落盘
            self._append_recent_block(ctx, block)
            ctx.history.consume_pending_into_user()
            # 一次原子写入：messages + recent_blocks + pending/delayed 一起落盘
            ctx.history.save()
            # 未发出的计划已注入本轮 user content，消费完毕
            ctx._unsent_intent = None

            # 记忆整理：只有本轮确实产出了 core/journal 才裁剪旧原文；
            # 没产出就保留原文，下次 cycle 继续提示整理（消息不丢失）
            if needs_compact:
                if wrote_memory:
                    keep_turns = max(1, self.config.trigger.history_keep_recent // 2)
                    ctx.history.trim_to_recent(keep_turns)
                else:
                    logger.warning(f"{ctx.chat_key} 记忆整理未产出 core/journal，保留原文，下次再试")

            # 7. 发送 + 归因（不持锁，发送期间群消息和 bot 回复都进 fast_buffer）
            self._handle_result(ctx, parsed, soft_factors)

            # 8. 检查重试：cycle 期间有新触发撞上则重跑（不传 soft_factors，归因跳过）
            #    - 若本轮 cycle 发了回复（last_reply_time 新近）：刚说过话，新消息攒着等静默窗口兜底
            #      （模拟真人"说完一轮先听一会"，不立即接话）
            #    - 若本轮 cycle 返回 silent（last_reply_time 较旧）：没说过话，新消息立即重跑
            #      （模拟真人"没开口时新消息值得看一眼"）
            #    - P003 v2.4：退出前检查"输出期间到达、LLM 没看过"的欠账。
            #      若已进入 direct（或私聊），武装回复倒计时，等对方停口后再回一次；
            #      未进入 direct 的群聊不武装，交给静默窗口/常规触发。
            soft_factors = None
            is_active = False
            if not self._consume_pending_trigger(ctx):
                self._maybe_arm_direct_countdown(ctx, new_speech=False)
                return
            if time.time() - ctx.last_reply_time < self._cooldown_seconds(ctx):
                logger.debug("刚回完消息，新触发等冷却结束（攒消息等下一轮）")
                self._maybe_arm_direct_countdown(ctx, new_speech=False)
                return
            logger.debug("上一轮 silent，立即重跑 cycle 处理撞锁的新触发")
            # 继续循环

    def _private_peer(self, ctx: ChatContext) -> dict:
        """私聊对方信息（供 persona 渲染 user content）。"""
        if ctx.chat_type != "private":
            return {}
        return {
            "qq": ctx.chat_id,
            "nickname": getattr(ctx, "peer_nickname", ctx.chat_id),
            "affinity": self.affinity.get(ctx.chat_id),
        }

    def _handle_result(self, ctx: ChatContext, parsed, soft_factors):
        """处理 LLM 结果：执行动作 -> 发送（可被打断）-> 写回历史 -> 亲密度 -> 归因。

        P005 打断模式：direct target（私聊为对方）在本轮 LLM 调用/发送期间发新内容时，
        停止尚未发出的消息，把未发出的计划存到 ctx._unsent_intent，下一轮重新组织语言。
        已发出的消息不撤回，也不重新发送；未发出的部分交给 LLM 自行决定继续说还是换话题。

        Args:
            ctx: 聊天上下文（群聊/私聊）
            parsed: 解析后的 LLM 输出
            soft_factors: 触发评分软因子（主动触发/私聊时为 None，归因跳过）
        """
        # 延迟：delay_seconds 叠加 0.3-1.2 秒/字的随机抖动（模拟"打字中"）
        total_delay = 0.0
        if parsed.delay_seconds > 0 or parsed.messages:
            total_text_len = sum(len(_msg_to_text(m)) for m in parsed.messages)
            jitter = random.uniform(0.3, 1.2) * max(1, total_text_len // 5)  # 每5字一抖动段
            total_delay = parsed.delay_seconds + min(jitter, 8.0)  # 抖动上限 8 秒

        if parsed.action == "silent":
            if total_delay > 0:
                time.sleep(total_delay)
            # 不发送，但仍更新归因（主动触发/私聊时 soft_factors=None，跳过）
            if soft_factors is not None:
                ctx.attribution.update(soft_factors, "silent")
            self.affinity.apply_delta(parsed.affinity_delta)
            # 阶段 F：silent 保留 direct 状态（仅群聊）
            if ctx.chat_type == "group":
                self._cleanup_expired_direct_targets(ctx)
            return

        if parsed.action == "react":
            if total_delay > 0:
                time.sleep(total_delay)
            # 调用 /set_msg_emoji_like 给目标消息加 emoji 反应
            msg_id = ctx.history.get_msg_id_by_id(parsed.react_target_msg_id)
            if not msg_id:
                logger.warning(
                    f"react 段 react_target_msg_id={parsed.react_target_msg_id} 无效，跳过 emoji 反应"
                )
            else:
                self.emoji_reactor.react(ctx.chat_type, ctx.chat_id, msg_id, parsed.react_emoji_id)
            if soft_factors is not None:
                ctx.attribution.update(soft_factors, "react")
            self.affinity.apply_delta(parsed.affinity_delta)
            # 阶段 F：react 保留 direct 状态（仅群聊）
            if ctx.chat_type == "group":
                self._cleanup_expired_direct_targets(ctx)
            return

        # ---------- reply / multi_reply：可被打断 ----------
        # 连续完整打断达到上限后，本次忽略打断、强制发出，避免对方一直打字导致 Bot 永不开口
        allow_interrupt = ctx._interrupt_streak < INTERRUPT_STREAK_LIMIT

        # 1) LLM 调用期间/进入本函数前已经到达的打断：整批都不发
        if allow_interrupt and ctx._send_interrupt.is_set():
            self._record_unsent_intent(ctx, parsed, 0, parsed.messages)
            ctx._interrupt_streak += 1
            ctx._send_interrupt.clear()
            logger.info(f"{ctx.chat_key} 发送前被打断：整批 {len(parsed.messages)} 条未发出，进入下一轮")
            return

        # 2) 发送前延迟（可被新消息提前唤醒）
        if total_delay > 0:
            logger.debug(f"延迟发送 {total_delay:.1f}s (delay={parsed.delay_seconds})")
            if allow_interrupt:
                if ctx._send_interrupt.wait(total_delay):
                    self._record_unsent_intent(ctx, parsed, 0, parsed.messages)
                    ctx._interrupt_streak += 1
                    ctx._send_interrupt.clear()
                    logger.info(f"{ctx.chat_key} 延迟期间被打断：整批 {len(parsed.messages)} 条未发出，进入下一轮")
                    return
            else:
                time.sleep(total_delay)

        # 3) 发送（multi_reply 的条间间隔同样可被打断）
        # 每条发送完成后会逐条 append 到 fast_buffer（is_bot=True），
        # 与此期间群成员的新消息按真实 append 顺序混合，下一轮 drain 时进入 pending。
        sent_count, unsent = self._send_messages(ctx, parsed.messages, allow_interrupt=allow_interrupt)
        if unsent:
            self._record_unsent_intent(ctx, parsed, sent_count, unsent)
            if sent_count == 0:
                logger.info(f"{ctx.chat_key} 发送中断：0 条已发，{len(unsent)} 条进入下一轮")
            else:
                logger.info(f"{ctx.chat_key} 发送中断：已发 {sent_count} 条，"
                            f"{len(unsent)} 条未发内容进入下一轮")
            # 只统计"完整打断"（一条都没发出的连续次数）
            ctx._interrupt_streak = ctx._interrupt_streak + 1 if sent_count == 0 else 0
        else:
            ctx._interrupt_streak = 0
        ctx._send_interrupt.clear()
        if not unsent:
            ctx._interrupt_sender = ""

        if sent_count == 0:
            # 整批被打断：不更新冷却/亲密度/归因/direct 状态，
            # 保留现有 direct 窗口与倒计时，让下一轮重规划能正常触发
            return

        ctx.last_reply_time = time.time()

        # 亲密度更新
        self.affinity.apply_delta(parsed.affinity_delta)

        # 归因更新（主动触发时 soft_factors=None，跳过）
        if soft_factors is not None:
            ctx.attribution.update(soft_factors, parsed.action)

        # P005：只有完整发出时才更新 direct 状态；部分打断时保留现有 direct 窗口/倒计时，
        # 让下一轮重规划继续（尤其避免 LLM 输出 open 把倒计时取消掉）
        if not unsent and ctx.chat_type == "group":
            self._update_direct_state(ctx, parsed)

    def _record_unsent_intent(self, ctx: ChatContext, parsed, sent_count: int, unsent: list):
        """记录被打断、尚未发出的计划，供下一轮 user content 注入。"""
        if not unsent:
            return
        ctx._unsent_intent = {
            "messages": list(unsent),
            "sent_count": sent_count,
            "planned_count": len(parsed.messages),
            "sender": ctx._interrupt_sender or "",
        }
        ctx._interrupt_sender = ""

    def _format_unsent_intent(self, ctx: ChatContext) -> str:
        """把未发出的计划渲染成 user content 里的一节（空则返回空串）。"""
        intent = ctx._unsent_intent
        if not intent:
            return ""
        unsent = intent.get("messages", [])
        if not unsent:
            return ""
        sender = intent.get("sender") or "对方"
        sent_count = intent.get("sent_count", 0)
        if sent_count <= 0:
            lines = ["你上一轮一条都没发出；原本准备发的内容如下："]
        else:
            lines = [f"你上一轮已经发出了前 {sent_count} 条；"
                     f"原本还准备继续发、但没有真的发到群里："]
        for m in unsent:
            lines.append(f"- {_msg_to_text(m)}")
        lines.append(f"你正要发的时候，{sender}又发了新内容（见下方最近消息）。")
        lines.append("请结合你原本想表达的意思和新内容重新组织语言：原计划还合适就接着说，"
                     "不合适就改说法；没话说就 silent。")
        return "\n".join(lines)

    # ---------- P006：最近对话块队列 ----------
    def _make_recent_block(self, ctx: ChatContext, is_active: bool, intent_text: str) -> dict:
        """用当前 pending 构造一个对话块（消息结构，不是渲染文本）。

        entries 与 pending_group_msgs 同结构；images 清空（历史/队列只保留 [图片] 文本）。
        user_content 不在这里存，落盘时用 entries 重新 render_user_content。
        """
        entries = [
            {**m, "images": []}
            for m in ctx.history.pending_group_msgs if isinstance(m, dict)
        ]
        return {
            "entries": entries,
            "assistant": "",
            "state": {},
            "is_active": is_active,
            "intent": intent_text,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M %A"),
            "finalized": False,
        }

    def _append_recent_block(self, ctx: ChatContext, block: dict):
        """块入队（不落盘）；超过窗口时最老块出队，由调用方统一 save。"""
        limit = max(1, int(getattr(ctx.config.trigger, "recent_pack_turns", 6)))
        evicted = ctx.history.append_recent_block(block, limit)
        if evicted is not None:
            self._persist_recent_block(ctx, evicted, save=False)

    def _recent_block_entries(self, ctx: ChatContext) -> list:
        """按块顺序展开队列内的消息结构（供当前请求拼接）。"""
        return ctx.history.get_recent_block_entries()

    def _render_recent_block_user_content(self, ctx: ChatContext, block: dict) -> str:
        """落盘时用消息结构重新渲染该块的 user 内容（不保存渲染文本）。"""
        entries = block.get("entries") if isinstance(block, dict) else None
        if not isinstance(entries, list):
            entries = []
        text = ctx.history.render_messages(entries)
        extra_qqs = {e.get("qq") for e in entries if isinstance(e, dict) and e.get("qq")}
        return self.persona_renderer.render_user_content(
            text, self._build_member_list(ctx, extra_qqs=extra_qqs),
            self.self_nickname, self.self_qq,
            is_active=block.get("is_active", False), chat_type=ctx.chat_type,
            peer=self._private_peer(ctx), interrupted_intent=block.get("intent", ""),
            for_storage=True, now_str=block.get("created_at", ""),
        )

    def _persist_recent_block(self, ctx: ChatContext, block: dict, save: bool = True) -> bool:
        """把块写入 conversation.json 的 history 序列（user/assistant 一对）。

        Args:
            save: 是否立即落盘；P006 成功路径会传 False，最后统一 save 一次

        Returns:
            True=已落盘；False=未完成/脏数据，跳过
        """
        if not isinstance(block, dict) or not block.get("finalized"):
            # 未完成的块不落盘；消息仍在 pending 里
            logger.warning(f"{ctx.chat_key} 未完成的对话块跳过落盘（保留 pending）")
            return False
        user_content = self._render_recent_block_user_content(ctx, block)
        assistant = block.get("assistant")
        if not isinstance(assistant, str):
            assistant = "" if assistant is None else str(assistant)
        ctx.history.append_turn(user_content, assistant, save=save)
        entries = block.get("entries")
        n = len(entries) if isinstance(entries, list) else 0
        logger.info(f"{ctx.chat_key} 最近对话块落盘：{n} 条消息")
        return True

    def _flush_recent_blocks(self, ctx: ChatContext):
        """清空某 chat 的队列并全部落盘（记忆整理前使用）。"""
        blocks = ctx.history.take_recent_blocks()
        persisted = 0
        for b in blocks:
            if self._persist_recent_block(ctx, b, save=False):
                persisted += 1
        if blocks:
            logger.info(f"{ctx.chat_key} 最近对话队列清空：{persisted}/{len(blocks)} 个块已落盘")
        # 一次原子写入：messages + queue(空) + pending/delayed
        ctx.history.save()

    def _update_direct_state(self, ctx: GroupContext, parsed):
        """阶段 F：根据 LLM 输出更新 direct 状态。

        仅在 action=reply/multi_reply 时调用（silent/react 在 _handle_result 中已处理）。

        - conversation_mode=direct + targets 至少一个可解析 QQ：
          对每个有效 target 设置 deadline = last_reply_time + DIRECT_WINDOW_SECONDS
          （保留字典中其他未过期 target，不清空已有）
        - conversation_mode=open 或 targets 全部不可解析：
          清空 direct_targets 字典，conversation_mode 回落 open
        """
        # 顺手清理过期 target
        self._cleanup_expired_direct_targets(ctx)

        if parsed.conversation_mode != "direct":
            # LLM 主动输出 open，退出 direct
            if ctx.direct_targets:
                logger.info(f"LLM 输出 conversation_mode=open，清空 direct_targets（{len(ctx.direct_targets)} 个）")
                ctx.direct_targets.clear()
            ctx.conversation_mode = "open"
            self._cancel_direct_countdown(ctx)
            return

        # conversation_mode=direct，解析 targets 为 QQ 列表
        valid_qqs = self._resolve_targets(ctx, parsed.targets)
        if not valid_qqs:
            # targets 全部不可解析，强制 open（无有效对话对象）
            logger.info("conversation_mode=direct 但 targets 无可解析 QQ，强制 open")
            ctx.direct_targets.clear()
            ctx.conversation_mode = "open"
            self._cancel_direct_countdown(ctx)
            return

        # 为每个有效 target 设置 deadline（保留字典中其他未过期 target）
        # 窗口起点用 last_reply_time（Bot 最后一条消息实际发送完成的时间戳），
        # 而非代码处理时间——避免绘图/媒体下载等慢发送压缩窗口
        deadline = ctx.last_reply_time + DIRECT_WINDOW_SECONDS
        for qq in valid_qqs:
            ctx.direct_targets[qq] = deadline
        ctx.conversation_mode = "direct"
        logger.info(f"更新 direct 状态：targets={list(ctx.direct_targets.keys())} "
                    f"deadline={deadline:.1f}（{DIRECT_WINDOW_SECONDS}s 窗口）")

    def _resolve_targets(self, ctx: GroupContext, targets: list) -> list:
        """解析 targets 字段为 QQ 列表。

        - 纯数字 → QQ 号
        - 非数字 → 成员表反查昵称→QQ（card 或 nickname 匹配）
        - 反查失败 → 跳过该元素

        Returns:
            有效 QQ 字符串列表
        """
        valid_qqs = []
        group_members = self.napcat.member_cache.get(ctx.group_id, {})
        for t in targets:
            t_str = str(t).strip()
            if not t_str:
                continue
            if t_str.isdigit():
                valid_qqs.append(t_str)
                continue
            # 昵称反查：card 优先于 nickname
            for qq, info in group_members.items():
                nick = info.get("card") or info.get("nickname") or ""
                if nick == t_str:
                    valid_qqs.append(qq)
                    break
        return valid_qqs

    def _cleanup_expired_direct_targets(self, ctx: GroupContext):
        """清理 direct_targets 字典中已过期的 target。

        若清理后字典为空，conversation_mode 自动回落 open。
        """
        now = time.time()
        expired = [qq for qq, deadline in ctx.direct_targets.items() if deadline <= now]
        for qq in expired:
            del ctx.direct_targets[qq]
        if expired:
            logger.debug(f"清理 {len(expired)} 个过期 direct target: {expired}")
        if not ctx.direct_targets and ctx.conversation_mode == "direct":
            ctx.conversation_mode = "open"
            logger.debug("direct_targets 全部过期，conversation_mode 回落 open")
            self._cancel_direct_countdown(ctx)

    def _direct_countdown_eligible(self, ctx: ChatContext) -> bool:
        """是否具备武装 direct 回复倒计时的条件。

        - 共同前提：fast_buffer 里有本轮 drain 之后到达、LLM 尚未看到的消息
        - 群聊：conversation_mode=direct 且至少一个 target 未过期
        - 私聊：恒为 1v1，只要有未看到的消息即可
        """
        if not ctx.history.has_unseen_buffered_messages():
            return False
        if ctx.chat_type == "private":
            return True
        if ctx.conversation_mode != "direct":
            return False
        now = time.time()
        snapshot = dict(ctx.direct_targets)  # dict 拷贝，避免跨线程迭代异常
        return any(deadline > now for deadline in snapshot.values())

    def _maybe_arm_direct_countdown(self, ctx: ChatContext, new_speech: bool):
        """武装/重置 direct 回复倒计时（P003 v2.4）。

        设计（替代 v2.1 的固定延迟补触发）：
        - Bot 输出期间到达的消息先打标记（_backlog_pending）；本轮结束若进入 direct
          （或私聊），武装倒计时，而不是固定延迟后无条件再调一次 LLM。
        - new_speech=True：direct target（私聊为对方）刚发新消息，重置倒计时；
          最多重置 DIRECT_COUNTDOWN_MAX_RESETS 次，之后不再重置，让当前倒计时自然到期。
        - new_speech=False：cycle 退出/撞冷却时补武装，不消耗重置次数。
        - 倒计时到期后一次性 drain 整批欠账，LLM 一次看到用户连发的全部内容，只回一次。

        并发：定时器回调在独立线程，_try_trigger_immediate 内部有 cycle 串行保护。
        """
        if not self._direct_countdown_eligible(ctx):
            # 条件不满足（已离开 direct / 没有未看过的消息）：撤销旧的倒计时
            self._cancel_direct_countdown(ctx)
            return

        with ctx._countdown_lock:
            if ctx._countdown_timer is not None:
                if not new_speech:
                    return  # 已武装，补检查不重复重置
                if ctx._countdown_resets >= DIRECT_COUNTDOWN_MAX_RESETS:
                    logger.debug(f"{ctx.chat_key} 倒计时重置已达上限 "
                                 f"{DIRECT_COUNTDOWN_MAX_RESETS} 次，不再重置")
                    return
                ctx._countdown_resets += 1
                ctx._countdown_timer.cancel()
            # 倒计时长度：至少等 target 停口 N 秒；若刚回复过，还要覆盖冷却
            delay = max(
                DIRECT_REPLY_COUNTDOWN_SECONDS,
                ctx.last_reply_time + self._cooldown_seconds(ctx) - time.time(),
            )
            ctx._countdown_gen += 1
            gen = ctx._countdown_gen
            timer = threading.Timer(delay, self._on_direct_countdown_timeout, args=(ctx, gen))
            timer.daemon = True
            timer.start()
            ctx._countdown_timer = timer
            resets = ctx._countdown_resets
        # 首次武装记 INFO，连发重置记 DEBUG，避免活跃对话刷日志
        log = logger.info if resets == 0 else logger.debug
        log(f"{ctx.chat_key} direct 回复倒计时武装：{delay:.1f}s 后触发"
            f"（resets={resets}/{DIRECT_COUNTDOWN_MAX_RESETS}"
            f"{'，backlog' if ctx._backlog_pending else ''}）")

    def _cancel_direct_countdown(self, ctx: ChatContext):
        """撤销 direct 回复倒计时并清理标记。"""
        with ctx._countdown_lock:
            if ctx._countdown_timer is not None:
                ctx._countdown_timer.cancel()
                ctx._countdown_timer = None
            ctx._countdown_gen += 1  # 使已进入回调的旧 gen 失效
            ctx._countdown_resets = 0
            ctx._backlog_pending = False

    def _on_direct_countdown_timeout(self, ctx: ChatContext, gen: int):
        """direct 回复倒计时到期：仍有未看过的消息才触发一次。"""
        try:
            with ctx._countdown_lock:
                if gen != ctx._countdown_gen:
                    return  # 已被重置/撤销，旧回调失效
                ctx._countdown_timer = None
                ctx._countdown_resets = 0
                ctx._backlog_pending = False
            if not self._direct_countdown_eligible(ctx):
                return
            logger.info(f"{ctx.chat_key} direct 回复倒计时到期，触发一轮")
            if ctx.chat_type == "private":
                self._try_trigger_private(ctx)
            else:
                self._try_trigger_immediate(ctx, direct=True)
        except Exception as e:
            logger.error(f"{ctx.chat_key} direct 回复倒计时异常: {e}", exc_info=True)

    def _send_messages(self, ctx: ChatContext, messages: list, allow_interrupt: bool = True) -> tuple[int, list]:
        """发送消息列表，带间隔；支持 P005 打断。

        每条消息发送完成后立即 append 到 fast_buffer（is_bot=True），
        与此期间接收线程写入的群消息按真实 append 顺序混合，
        保证下一轮 LLM 看到的发言顺序 = 真实群聊发言顺序。
        multi_reply 逐条 append，中间间隔期间群成员插话会自然夹在 bot 消息之间。

        Args:
            messages: LLM 输出的消息计划
            allow_interrupt: 是否允许在发送过程中被 direct target 的新发言打断

        Returns:
            (sent_count, unsent_messages)
            sent_count: 实际成功发出的条数；unsent_messages: 因打断而未发出的剩余计划
        """
        # 预先配对"原始 message -> 消息段"，避免 build_segments 丢弃空消息导致下标错位
        pairs = []
        for msg in messages:
            segs_list = self.message_sender.build_segments([msg], ctx.history)
            if segs_list:
                pairs.append((msg, segs_list[0]))

        sent_count = 0
        for i, (msg, segs) in enumerate(pairs):
            # 发送前检查打断（已在发送窗口内）
            if allow_interrupt and ctx._send_interrupt.is_set():
                return sent_count, [m for m, _ in pairs[i:]]

            # 处理特殊段
            handled = False
            sent_ok = True  # 特殊段发送是否成功；失败时不 append 到 fast_buffer（保持历史事实性）
            for seg in segs:
                if seg.get("type") == "forward":
                    data = seg.get("data", {})
                    resp = self.napcat.send_forward(
                        ctx.chat_type, ctx.chat_id,
                        data.get("messages", []), data.get("title", ""),
                    )
                    handled = True
                    sent_ok = bool(resp)
                    break
                if seg.get("type") == "image":
                    sent_ok = self.image_sender.send(ctx.chat_type, ctx.chat_id, seg.get("data", {}))
                    handled = True
                    break
                if seg.get("type") == "video":
                    sent_ok = self.video_sender.send(ctx.chat_type, ctx.chat_id, seg.get("data", {}))
                    handled = True
                    break
                if seg.get("type") == "voice":
                    data = seg.get("data", {})
                    channel = data.get("channel", "ai_record")
                    if channel == "ai_record":
                        sent_ok = self.ai_voice_sender.send(ctx.chat_type, ctx.chat_id, data)
                    elif channel == "local_file":
                        sent_ok = self.local_voice_sender.send(ctx.chat_type, ctx.chat_id, data)
                    elif channel == "url":
                        sent_ok = self.url_voice_sender.send(ctx.chat_type, ctx.chat_id, data)
                    handled = True
                    break
            if handled:
                if sent_ok:
                    # 特殊段记入 fast_buffer（语音/图片/视频/转发都算一条 bot 发言）
                    self._append_bot_reply_to_buffer(ctx, msg)
                    sent_count += 1
                else:
                    logger.warning("特殊段发送失败，不记录到历史（保持历史事实性）")
            else:
                # 普通消息段（私聊不需要 @，过滤 at 段避免意外 @ 对方）
                normal_segs = [s for s in segs if s.get("type") not in ("forward", "image", "video", "voice")]
                if ctx.chat_type == "private":
                    normal_segs = [s for s in normal_segs if s.get("type") != "at"]
                if normal_segs:
                    self.message_sender.send(ctx.chat_type, ctx.chat_id, normal_segs)
                # 逐条 append 到 fast_buffer（与群消息按真实时间顺序混合）
                self._append_bot_reply_to_buffer(ctx, msg)
                sent_count += 1

            # 多条消息间隔（可被新发言提前唤醒）
            if i < len(pairs) - 1:
                gap = random.uniform(0.8, 2.5)
                if allow_interrupt:
                    if ctx._send_interrupt.wait(gap):
                        return sent_count, [m for m, _ in pairs[i + 1:]]
                else:
                    time.sleep(gap)

        return sent_count, []

    def _append_bot_reply_to_buffer(self, ctx: ChatContext, msg):
        """把单条 bot 发言追加到 fast_buffer（is_bot=True）。

        在 _send_messages 每条发送完成后调用，确保发言顺序与真实群聊一致。
        文本摘要复用 _msg_to_text，特殊段（image/voice/forward）也能得到合理摘要。
        """
        msg_text = _msg_to_text(msg)
        ctx.history.append_group_message(
            self.self_qq, self.self_nickname, msg_text, "", is_bot=True
        )

    def _build_member_list(self, ctx: ChatContext, extra_qqs=None) -> list:
        """构建传给 LLM 的群成员列表（含亲密度）。

        私聊没有群成员概念，返回空列表；user content 由 persona 渲染对方信息。
        extra_qqs: 额外纳入"近期活跃"的 QQ（块出队落盘时带上该块自己的发言者）。
        """
        if ctx.chat_type == "private":
            return []
        recent_speakers = ctx.history.get_recent_speakers()
        # P006：最近对话块在 history.recent_blocks 里，尚未写入 messages；把它们里的发言者也纳入近期活跃
        blocks = ctx.history.get_recent_blocks()
        for b in blocks:
            if not isinstance(b, dict):
                continue
            entries = b.get("entries")
            if not isinstance(entries, list):
                continue
            for e in entries:
                if isinstance(e, dict) and e.get("qq"):
                    recent_speakers.add(e["qq"])
        # 当前 pending 尚未入队，其发言者也要纳入近期活跃
        for e in ctx.history.pending_group_msgs:
            if isinstance(e, dict) and e.get("qq"):
                recent_speakers.add(e["qq"])
        if extra_qqs:
            recent_speakers.update(q for q in extra_qqs if q)
        group_members = self.napcat.member_cache.get(ctx.group_id, {})
        result = []
        for qq, info in group_members.items():
            affinity = self.affinity.get(qq)
            # 过滤：近期发言过 / 有亲密度 / 是自己
            if qq not in recent_speakers and affinity <= 0 and qq != self.self_qq:
                continue
            result.append({
                "qq": qq,
                "nickname": info.get("card") or info.get("nickname") or qq,
                "role": info.get("role", "member"),
                "affinity": affinity,
            })
        return result

    def run(self, webhook_host: str = "0.0.0.0", webhook_port: int = 8081):
        """启动机器人。"""
        self.warmup()   # 阶段 D：per-group 调度器在 warmup 中启动
        server = NapCatWebhookServer(
            webhook_host, webhook_port, self.on_group_message,
            on_recall=self.on_group_recall,
            on_poke=self.on_group_poke,
            on_private_message=self.on_private_message,
            on_private_recall=self.on_private_recall,
            on_private_poke=self.on_private_poke,
        )
        private_info = ""
        if self.config.private and self.config.private.enabled:
            private_info = f"，私聊白名单 {self.config.private.allow_list}"
        logger.info(f"机器人启动，监听群 {list(self.groups.keys())}{private_info}")
        server.start()


def _extract_text_from_msg(msg: dict) -> str:
    """从消息段提取纯文本。"""
    parts = []
    for seg in msg.get("message", []):
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


# 阶段 G：内心状态维度的日志缩写
_STATE_ABBR = {
    "interest": "int",
    "annoyance": "ann",
    "urge_to_speak": "urge",
    "social_risk": "risk",
    "energy": "eng",
    "banter": "ban",
}


def _format_state(state: dict) -> str:
    """把结构化内心状态压成一行日志（阶段 G，用于观测维度分布）。"""
    if not isinstance(state, dict):
        return "无"
    dims = state.get("dims")
    if not isinstance(dims, dict):
        dims = {}
    dim_str = " ".join(f"{abbr}={dims.get(key, 0.5)}" for key, abbr in _STATE_ABBR.items())
    mode = state.get("mode") or "无"
    return f"mode={mode} {dim_str} note={state.get('note', '')}"


def _msg_to_text(msg) -> str:
    """消息转文本摘要（写回历史用）。"""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        # 多段混合消息：逐段拼接摘要
        return "".join(_msg_to_text(seg) for seg in msg)
    if isinstance(msg, dict):
        t = msg.get("type", "")
        d = msg.get("data", {})
        if t == "text":
            return d.get("text", "")
        if t == "at":
            return f"@{d.get('qq', '')}"
        if t == "face":
            return f"[face:{d.get('id', '')}]"
        if t == "image":
            return "[图片]"
        if t == "reply":
            return f"[引用回复]{d.get('text', '')}"
        if t == "voice":
            return f"[语音:{d.get('text', '')}]"
        return f"[{t}]"
    return ""


def _install_excepthooks():
    """安装全局异常钩子，确保未捕获异常（含子线程）写入日志。

    排查程序静默退出问题用：默认情况下子线程未捕获异常只打到 stderr，
    若 stderr 未重定向到日志文件则丢失。此处统一捕获并记 CRITICAL。
    """
    import sys

    def _sys_excepthook(exc_type, exc_value, tb):
        try:
            logger.critical("主线程未捕获异常", exc_info=(exc_type, exc_value, tb))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, tb)

    def _thread_excepthook(args):
        try:
            logger.critical(
                f"子线程未捕获异常: thread={args.thread.name if args.thread else '?'}",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
            )
        except Exception:
            pass

    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook
    logger.info("全局异常钩子已安装（sys.excepthook + threading.excepthook）")


if __name__ == "__main__":
    _install_excepthooks()
    try:
        bot = Bot()
        bot.run()
    except Exception:
        logger.critical("Bot 运行异常退出", exc_info=True)
        raise
