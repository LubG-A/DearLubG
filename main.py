"""QQ 群聊机器人主入口。

流程：
- 被动触发：群聊消息 -> 入 fast_buffer -> 评分 -> 触发决策（硬因子短冷却立即 / 软因子静默窗口兜底）
              -> LLM 调用（_cycle_running 串行）-> drain buffer 到 pending -> 结果处理
- 主动触发：定时器唤醒 -> LLM 调用（无新消息，让 LLM 自主决定要不要主动开口）
- 延迟回复：LLM 输出 reply_delay_minutes -> 消息暂存 -> 下次触发时重新加入 pending

并发模型（B2 方案）：
- 接收线程（webhook）只写 fast_buffer（HistoryManager 内 _buffer_lock 保护，持锁极短）
- LLM 工作线程用 _cycle_running 标志保证串行，LLM 调用和发送不持锁
- _cycle_lock 只保护"检查+设置 _cycle_running"（持锁极短），不覆盖 LLM 调用
- 撞 cycle 时注册 ctx._cycle_pending（per-group），cycle 完成后立即重跑（不等静默窗口 20 秒）
- 静默窗口定时器：每条新消息重置，150s 无新消息则触发兜底 LLM 调用

阶段 A 重构：per-group 状态移入 GroupContext（src/group_context.py），
Bot 持有 self.groups: dict[str, GroupContext]，方法通过 ctx 参数访问 per-group 状态。
全局共享单例（napcat/llm/affinity/persona/senders）仍留在 Bot。

阶段 B：多群配置（config.group_ids）+ 全局 cycle 队列。
- NapCatClient 方法接受 group_id 参数，member_cache 嵌套化 {group_id: {qq: info}}
- webhook 去掉单群过滤，按 group_id 路由到对应 ctx
- per-group 文件存储：state/{group_id}/ 子目录
- _cycle_queue + _cycle_queue_set：cycle 结束后处理其他撞 cycle 的群
- 主动触发关闭（阶段 D 每群独立倒计时后开启）
"""
import time
import random
import threading
from collections import deque
from typing import Optional

from src.config import load_config
from src.napcat_client import NapCatClient, NapCatWebhookServer
from src.llm_client import LLMClient
from src.affinity import AffinityManager
from src.persona import PersonaRenderer
from src.parser import parse_and_validate
from src.senders.message_sender import NapCatMessageSender
from src.senders.voice_sender import AIRecordVoiceSender, LocalFileVoiceSender, UrlVoiceSender
from src.senders.image_sender import NapCatImageSender
from src.senders.video_sender import NapCatVideoSender
from src.senders.emoji_reactor import EmojiReactor
from src.group_context import GroupContext
from src.utils.logger import get_logger

logger = get_logger("main")

# 冷却配置
HARD_COOLDOWN_SECONDS = 3       # 硬因子（@/提问）触发冷却，距上次回复 <5s 推迟
SOFT_COOLDOWN_MIN = 5         # 软因子触发冷却下限
SOFT_COOLDOWN_MAX = 60         # 软因子触发冷却上限
QUIET_WINDOW_SECONDS = 150       # 静默窗口：N 秒无新消息后兜底触发
DIRECT_WINDOW_SECONDS = 45      # 阶段 F：direct 模式活跃窗口，target 在此窗口内发言触发快速触发


class Bot:
    """机器人主控制器。"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)

        # 全局共享单例（napcat / llm / affinity / persona / senders）
        # 阶段 D：active_scheduler 移入 GroupContext（per-group 独立倒计时）
        self.napcat = NapCatClient(self.config.napcat.base_url, self.config.napcat.group_ids)
        self.llm = LLMClient(self.config)
        self.affinity = AffinityManager()  # 全局按 QQ（跨群身份一致）
        self.persona_renderer = PersonaRenderer(self.config)

        # Sender 实现（全局共享）
        self.message_sender = NapCatMessageSender(self.napcat)
        self.ai_voice_sender = AIRecordVoiceSender(
            self.napcat, self.config.voice.ai_record_character, self.config.voice.fallback_to_text
        )
        self.local_voice_sender = LocalFileVoiceSender(self.napcat)
        # 网络资源 sender 注入下载超时与临时目录（从 config.media_download 读取，缺失用默认值）
        md = getattr(self.config, "media_download", None)
        md_timeout = md.timeout if md else 30
        md_video_timeout = md.video_timeout if md else 60
        md_temp_dir = md.temp_dir if md else "media/downloaded"
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
        # 全局 cycle 队列：cycle 结束后处理其他撞 cycle 的群（阶段 B）
        self._cycle_queue: deque[str] = deque()
        self._cycle_queue_set: set[str] = set()

        # per-group 上下文表：{group_id -> GroupContext}
        # 阶段 A 单群，warmup 时填充一个 GroupContext
        # 阶段 B 起支持多群，启动时为每个配置的 group_id 构造一个
        self.groups: dict[str, GroupContext] = {}

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
                llm=self.llm,
                affinity=self.affinity,
                self_qq=self.self_qq,
                persona_name=self.config.persona.name,
            )
            self.groups[group_id] = ctx

        # 探测 AI 语音 character 是否可用（不阻塞启动）
        self._probe_ai_voice_character()
        logger.info(f"预热完成，机器人 {self.self_nickname}({self.self_qq})，"
                    f"群 {list(self.groups.keys())}")

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
            content = msg.get("raw_message", "") or _extract_text_from_msg(msg)
            msg_id = str(msg.get("message_id", ""))

            # 语音消息：先用占位入历史（不阻塞），独立线程延迟重试转写后回填
            # NapCat 收到语音需先从腾讯下载 amr 文件，立即调用 fetch_ptt_text 会因文件未就绪而失败
            has_voice = self._has_voice_segment(msg)
            if has_voice:
                content = (content + (" " if content else "") + "[语音消息]").strip()

            # 1. 入 fast_buffer（无 _cycle_lock，仅 HistoryManager 内 _buffer_lock）
            ctx.history.append_group_message(sender_qq, sender_nick, content, msg_id)

            # 语音转写：异步延迟重试，成功后回填历史 content
            if has_voice and msg_id:
                threading.Thread(target=self._transcribe_voice_async, args=(ctx, msg_id), daemon=True).start()

            # 2. 评分（接收线程，只读）
            score, soft_factors = ctx.trigger_evaluator.evaluate(msg)
            is_hard = self._is_hard_trigger(ctx, msg, content)
            logger.debug(f"消息评分={score} hard={is_hard} soft_factors={[f.name for f in soft_factors]}")

            # 3. 触发决策（按优先级：硬因子 > direct 快速触发 > 软因子 > 静默兜底）
            if is_hard:
                self._try_trigger_immediate(ctx, hard=True)
            else:
                # 阶段 F：direct 快速触发——发送者在 direct_targets 字典中且未过该 target 的 deadline
                # 绕过 peek_threshold，直接让 LLM 看一眼（LLM 仍决定 silent/reply）
                now_ts = time.time()
                direct_deadline = ctx.direct_targets.get(sender_qq)
                if direct_deadline is not None and direct_deadline > now_ts:
                    # 续命：重置该 target 的 deadline
                    ctx.direct_targets[sender_qq] = now_ts + DIRECT_WINDOW_SECONDS
                    self._try_trigger_immediate(ctx, direct=True)
                elif ctx.trigger_evaluator.should_peek(score):
                    self._try_trigger_immediate(ctx, hard=False, soft_factors=soft_factors)
                # 低分消息不立即触发，等静默窗口兜底

            # 4. 重置静默定时器（无论硬软，新消息都推迟兜底触发）
            self._reschedule_quiet_trigger(ctx)

        except Exception as e:
            logger.error(f"处理消息异常: {e}", exc_info=True)

    def _has_voice_segment(self, msg: dict) -> bool:
        """消息是否含语音段（record）。"""
        return any(seg.get("type") == "record" for seg in msg.get("message", []))

    def _transcribe_voice_async(self, ctx: GroupContext, msg_id: str):
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

    def _try_enter_cycle(self, ctx: GroupContext) -> bool:
        """尝试进入 LLM cycle（B2 方案）。

        用 _cycle_running 标志保证 cycle 串行，代替 _llm_lock 覆盖全流程。
        _cycle_lock 只保护"检查+设置"（持锁极短），LLM 调用和发送不持锁。

        Args:
            ctx: 群上下文。撞 cycle 时把 _cycle_pending 标记到该 ctx（per-group）。

        Returns:
            True: 成功进入 cycle（调用方负责执行 _run_llm_cycle + _exit_cycle）
            False: 已有 cycle 在跑，已注册 ctx._cycle_pending 重试（调用方直接返回）
        """
        with self._cycle_lock:
            if self._cycle_running:
                ctx._cycle_pending = True
                self._enqueue_group(ctx.group_id)
                return False
            self._cycle_running = True
            return True

    def _enqueue_group(self, group_id: str):
        """群入队 cycle 队列（去重）。在 _cycle_lock 保护下调用。"""
        if group_id not in self._cycle_queue_set:
            self._cycle_queue.append(group_id)
            self._cycle_queue_set.add(group_id)

    def _exit_cycle(self):
        """退出 LLM cycle，清除运行标志。"""
        with self._cycle_lock:
            self._cycle_running = False

    def _drain_cycle_queue(self):
        """处理 cycle 队列：cycle 结束后处理其他撞 cycle 的群。

        迭代处理（非递归），避免多群排队时递归过深。
        每次从队首取一个群，执行 _run_llm_cycle，结束后继续取下一个。
        """
        while self._cycle_queue:
            group_id = self._cycle_queue.popleft()
            self._cycle_queue_set.discard(group_id)
            ctx = self.groups.get(group_id)
            if ctx is None or not ctx._cycle_pending:
                continue  # 已被消费或不存在

            with self._cycle_lock:
                if self._cycle_running:
                    # 不应该发生（_cycle_running 刚清），防御性重新入队
                    self._cycle_queue.appendleft(group_id)
                    self._cycle_queue_set.add(group_id)
                    return
                self._cycle_running = True

            try:
                logger.debug(f"从队列处理群 {group_id} 的 pending cycle")
                self._run_llm_cycle(ctx, soft_factors=None, is_active=False)
            finally:
                self._exit_cycle()

    def _consume_pending_trigger(self, ctx: GroupContext) -> bool:
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

    def _try_trigger_immediate(self, ctx: GroupContext, hard: bool = False, soft_factors=None, direct: bool = False):
        """立即尝试触发 LLM（硬因子 / direct 快速 / 软因子路径）。

        冷却检查：距上次回复 < cooldown 则跳过（让静默窗口兜底）。
        cycle 串行检查：撞 _cycle_running 则注册重试，cycle 完成后立即重跑。

        Args:
            ctx: 群上下文（per-group 冷却时间和 _cycle_pending）
            hard: True=硬因子触发（@/提问），False=非硬因子
            soft_factors: 软因子触发时传入命中的软因子列表，供归因使用；
                         硬因子/direct 触发传 None，归因跳过。
            direct: True=direct 快速触发（阶段 F，绕过 peek_threshold，归因跳过）
        """
        now = time.time()
        elapsed = now - ctx.last_reply_time
        if direct:
            cooldown = SOFT_COOLDOWN_MIN
            trigger_label = "direct快速"
        elif hard:
            cooldown = HARD_COOLDOWN_SECONDS
            trigger_label = "硬因子"
        else:
            cooldown = SOFT_COOLDOWN_MIN
            trigger_label = "软因子"
        if elapsed < cooldown:
            logger.debug(f"{trigger_label}触发但冷却中（elapsed={elapsed:.1f}s < {cooldown}s），等静默窗口兜底")
            return

        if not self._try_enter_cycle(ctx):
            logger.debug(f"{trigger_label}触发但 cycle 在跑，已入队等待")
            return

        try:
            self._run_llm_cycle(ctx, soft_factors=soft_factors, is_active=False)
        finally:
            self._exit_cycle()
            self._drain_cycle_queue()

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
                self._exit_cycle()
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
                self._exit_cycle()
                self._drain_cycle_queue()
        except Exception as e:
            logger.error(f"群 {group_id} 主动触发异常: {e}", exc_info=True)

    def _run_llm_cycle(self, ctx: GroupContext, soft_factors, is_active: bool):
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
                return

            # 4. 构建 user_content（创建快照供 reply/react 段引用）
            summary = ctx.history.get_summary()
            system_prompt = self.persona_renderer.render_system_prompt(summary)
            history_messages = ctx.history.get_messages_for_llm()
            member_list = self._build_member_list(ctx)
            pending_text = ctx.history.build_user_content()
            new_user_content = self.persona_renderer.render_user_content(
                pending_text, member_list, self.self_nickname, self.self_qq,
                is_active=is_active,
            )

            # 5. 调用 LLM（不持锁，_cycle_running 标志保证串行）
            raw_result = self.llm.chat(system_prompt, history_messages, new_user_content)
            if raw_result is None:
                logger.warning("LLM 调用失败，本轮跳过")
                # pending 留待下次触发再拼入（消息不丢失）
                if self._consume_pending_trigger(ctx):
                    continue
                return

            # 6. 解析 + 落地
            parsed = parse_and_validate(raw_result)
            logger.info(f"LLM 返回 action={parsed.action} thought={parsed.thought[:50]}"
                        f"{' reply_delay=' + str(parsed.reply_delay_minutes) + 'min' if parsed.reply_delay_minutes > 0 else ''}"
                        f"{' [主动触发]' if is_active else ''}")

            # 延迟回复处理：LLM 觉得"等会再回"，把这批消息存到 delayed_replies
            if parsed.reply_delay_minutes > 0 and parsed.action == "silent":
                ctx.history.stash_pending_as_delayed(parsed.reply_delay_minutes)
                if self._consume_pending_trigger(ctx):
                    continue
                return

            # 清空 pending（user_content 已在步骤 4 构建，不重新 build）
            ctx.history.consume_pending_into_user()
            ctx.history.append_turn(new_user_content, raw_result)

            # 7. 发送 + 归因（不持锁，发送期间群消息和 bot 回复都进 fast_buffer）
            self._handle_result(ctx, parsed, soft_factors)

            # 8. 检查重试：cycle 期间有新触发撞上则重跑（不传 soft_factors，归因跳过）
            #    - 若本轮 cycle 发了回复（last_reply_time 新近）：刚说过话，新消息攒着等静默窗口兜底
            #      （模拟真人"说完一轮先听一会"，不立即接话）
            #    - 若本轮 cycle 返回 silent（last_reply_time 较旧）：没说过话，新消息立即重跑
            #      （模拟真人"没开口时新消息值得看一眼"）
            soft_factors = None
            is_active = False
            if not self._consume_pending_trigger(ctx):
                return
            if time.time() - ctx.last_reply_time < SOFT_COOLDOWN_MIN:
                logger.debug("刚回完消息，新触发等静默窗口兜底（攒消息等下一轮）")
                return
            logger.debug("上一轮 silent，立即重跑 cycle 处理撞锁的新触发")
            # 继续循环

    def _handle_result(self, ctx: GroupContext, parsed, soft_factors):
        """处理 LLM 结果：执行动作 -> 写回历史 -> 亲密度 -> 归因。

        Args:
            ctx: 群上下文（per-group history/attribution/last_reply_time/group_id）
            parsed: 解析后的 LLM 输出
            soft_factors: 触发评分软因子（主动触发时为 None，归因跳过）
        """
        # 延迟：delay_seconds 叠加 0.3-1.2 秒/字的随机抖动（模拟"打字中"）
        if parsed.delay_seconds > 0 or parsed.messages:
            total_text_len = sum(len(_msg_to_text(m)) for m in parsed.messages)
            jitter = random.uniform(0.3, 1.2) * max(1, total_text_len // 5)  # 每5字一抖动段
            total_delay = parsed.delay_seconds + min(jitter, 8.0)  # 抖动上限 8 秒
            if total_delay > 0:
                logger.debug(f"延迟发送 {total_delay:.1f}s (delay={parsed.delay_seconds} jitter={jitter:.1f})")
                time.sleep(total_delay)

        if parsed.action == "silent":
            # 不发送，但仍更新归因（主动触发时 soft_factors=None，跳过）
            if soft_factors is not None:
                ctx.attribution.update(soft_factors, "silent")
            self.affinity.apply_delta(parsed.affinity_delta)
            # 阶段 F：silent 保留 direct 状态（不清空 direct_targets）
            # 只顺手清理过期 target
            self._cleanup_expired_direct_targets(ctx)
            return

        if parsed.action == "react":
            # 调用 /set_msg_emoji_like 给目标消息加 emoji 反应
            msg_id = ctx.history.get_msg_id_by_id(parsed.react_target_msg_id)
            if not msg_id:
                logger.warning(
                    f"react 段 react_target_msg_id={parsed.react_target_msg_id} 无效，跳过 emoji 反应"
                )
            else:
                self.emoji_reactor.react(ctx.group_id, msg_id, parsed.react_emoji_id)
            if soft_factors is not None:
                ctx.attribution.update(soft_factors, "react")
            self.affinity.apply_delta(parsed.affinity_delta)
            # 阶段 F：react 保留 direct 状态（同 silent），清理过期 target
            self._cleanup_expired_direct_targets(ctx)
            return

        # reply / multi_reply
        # _send_messages 内部每条发送完成后会逐条 append 到 fast_buffer（is_bot=True），
        # 与此期间群成员的新消息按真实 append 顺序混合，下一轮 drain 时进入 pending。
        self._send_messages(ctx, parsed.messages)
        ctx.last_reply_time = time.time()

        # 亲密度更新
        self.affinity.apply_delta(parsed.affinity_delta)

        # 归因更新（主动触发时 soft_factors=None，跳过）
        if soft_factors is not None:
            ctx.attribution.update(soft_factors, parsed.action)

        # 阶段 F：根据 LLM 输出更新 direct 状态
        # - conversation_mode=direct + targets 至少一个可解析 → 为每个 target 设置 deadline
        # - conversation_mode=open 或 targets 全部不可解析 → 清空 direct_targets
        # - silent/react 已在上面处理（保留 direct 状态）
        self._update_direct_state(ctx, parsed)

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
            return

        # conversation_mode=direct，解析 targets 为 QQ 列表
        valid_qqs = self._resolve_targets(ctx, parsed.targets)
        if not valid_qqs:
            # targets 全部不可解析，强制 open（无有效对话对象）
            logger.info("conversation_mode=direct 但 targets 无可解析 QQ，强制 open")
            ctx.direct_targets.clear()
            ctx.conversation_mode = "open"
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

    def _send_messages(self, ctx: GroupContext, messages: list):
        """发送消息列表，带间隔。

        每条消息发送完成后立即 append 到 fast_buffer（is_bot=True），
        与此期间接收线程写入的群消息按真实 append 顺序混合，
        保证下一轮 LLM 看到的发言顺序 = 真实群聊发言顺序。
        multi_reply 逐条 append，中间间隔期间群成员插话会自然夹在 bot 消息之间。
        """
        segments_list = self.message_sender.build_segments(messages, ctx.history)
        for i, segs in enumerate(segments_list):
            # 处理特殊段
            handled = False
            sent_ok = True  # 特殊段发送是否成功；失败时不 append 到 fast_buffer（保持历史事实性）
            for seg in segs:
                if seg.get("type") == "forward":
                    data = seg.get("data", {})
                    resp = self.napcat.send_group_forward_msg(ctx.group_id, data.get("messages", []), data.get("title", ""))
                    handled = True
                    sent_ok = bool(resp)
                    break
                if seg.get("type") == "image":
                    sent_ok = self.image_sender.send(ctx.group_id, seg.get("data", {}))
                    handled = True
                    break
                if seg.get("type") == "video":
                    sent_ok = self.video_sender.send(ctx.group_id, seg.get("data", {}))
                    handled = True
                    break
                if seg.get("type") == "voice":
                    data = seg.get("data", {})
                    channel = data.get("channel", "ai_record")
                    if channel == "ai_record":
                        sent_ok = self.ai_voice_sender.send(ctx.group_id, data)
                    elif channel == "local_file":
                        sent_ok = self.local_voice_sender.send(ctx.group_id, data)
                    elif channel == "url":
                        sent_ok = self.url_voice_sender.send(ctx.group_id, data)
                    handled = True
                    break
            if handled:
                if sent_ok:
                    # 特殊段记入 fast_buffer（语音/图片/视频/转发都算一条 bot 发言）
                    self._append_bot_reply_to_buffer(ctx, messages[i])
                else:
                    logger.warning("特殊段发送失败，不记录到历史（保持历史事实性）")
                continue

            # 普通消息段
            normal_segs = [s for s in segs if s.get("type") not in ("forward", "image", "video", "voice")]
            if normal_segs:
                self.message_sender.send_group_message(ctx.group_id, normal_segs)

            # 逐条 append 到 fast_buffer（与群消息按真实时间顺序混合）
            self._append_bot_reply_to_buffer(ctx, messages[i])

            # 多条消息间隔
            if i < len(segments_list) - 1:
                time.sleep(random.uniform(0.8, 2.5))

    def _append_bot_reply_to_buffer(self, ctx: GroupContext, msg):
        """把单条 bot 发言追加到 fast_buffer（is_bot=True）。

        在 _send_messages 每条发送完成后调用，确保发言顺序与真实群聊一致。
        文本摘要复用 _msg_to_text，特殊段（image/voice/forward）也能得到合理摘要。
        """
        msg_text = _msg_to_text(msg)
        ctx.history.append_group_message(
            self.self_qq, self.self_nickname, msg_text, "", is_bot=True
        )

    def _build_member_list(self, ctx: GroupContext) -> list:
        """构建传给 LLM 的群成员列表（含亲密度）。

        过滤规则：只展示以下成员（避免 user content 顶部塞入全群名单）：
        - 最近 N 轮发言过的成员（recent_speakers，从 ctx.history 派生）
        - 有亲密度记录的成员（affinity > 0，全局共享）
        - 自己（self）
        """
        recent_speakers = ctx.history.get_recent_speakers()
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
        )
        logger.info(f"机器人启动，监听群 {list(self.groups.keys())}")
        server.start()


def _extract_text_from_msg(msg: dict) -> str:
    """从消息段提取纯文本。"""
    parts = []
    for seg in msg.get("message", []):
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


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
