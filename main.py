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
- 撞 cycle 时注册 _cycle_pending，cycle 完成后立即重跑（不等静默窗口 20 秒）
- 静默窗口定时器：每条新消息重置，20s 无新消息则触发兜底 LLM 调用
"""
import time
import random
import threading
from typing import Optional

from src.config import load_config
from src.napcat_client import NapCatClient, NapCatWebhookServer
from src.llm_client import LLMClient
from src.history import HistoryManager
from src.trigger import TriggerEvaluator
from src.attribution import AttributionManager
from src.affinity import AffinityManager
from src.persona import PersonaRenderer
from src.parser import parse_and_validate
from src.scheduler import ActiveScheduler
from src.senders.message_sender import NapCatMessageSender
from src.senders.voice_sender import AIRecordVoiceSender, LocalFileVoiceSender
from src.senders.image_sender import EmptyImageSender
from src.senders.emoji_reactor import EmptyEmojiReactor
from src.utils.logger import get_logger

logger = get_logger("main")

# 冷却配置
HARD_COOLDOWN_SECONDS = 5       # 硬因子（@/提问）触发冷却，距上次回复 <2s 推迟
SOFT_COOLDOWN_MIN = 10         # 软因子触发冷却下限
SOFT_COOLDOWN_MAX = 60         # 软因子触发冷却上限
QUIET_WINDOW_SECONDS = 150       # 静默窗口：N 秒无新消息后兜底触发


class Bot:
    """机器人主控制器。"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)

        # 核心组件
        self.napcat = NapCatClient(self.config.napcat.base_url, self.config.napcat.group_id)
        self.llm = LLMClient(self.config)
        self.history = HistoryManager(self.config.trigger)
        # 注入 LLM 摘要器：启用方案A分层压缩（中期 LLM 摘要 + 远期朴素摘要）
        self.history.set_summarizer(self.llm.summarize)
        self.attribution = AttributionManager(self.config)
        self.affinity = AffinityManager()
        self.persona_renderer = PersonaRenderer(self.config)
        self.scheduler = ActiveScheduler(self.config)

        # Sender 实现
        self.message_sender = NapCatMessageSender(self.napcat)
        self.ai_voice_sender = AIRecordVoiceSender(
            self.napcat, self.config.voice.ai_record_character, self.config.voice.fallback_to_text
        )
        self.local_voice_sender = LocalFileVoiceSender(self.napcat)
        self.image_sender = EmptyImageSender()
        self.emoji_reactor = EmptyEmojiReactor()

        # 运行时状态
        self.self_qq: str = ""
        self.self_nickname: str = ""
        self.trigger_evaluator: Optional[TriggerEvaluator] = None
        self.last_reply_time: float = 0.0
        # LLM cycle 串行控制（B2 方案）：用标志代替 _llm_lock 保证 cycle 串行，
        # LLM 调用和发送不持锁，缩短"撞锁"窗口。
        # _cycle_lock 只保护"检查+设置 _cycle_running"（持锁极短），不覆盖 LLM 调用。
        # _cycle_running: True 表示有 LLM cycle 在跑，新触发撞标志后注册重试
        # _cycle_pending: True 表示有触发在 cycle 期间撞上，cycle 完成后立即重跑
        self._cycle_lock = threading.Lock()
        self._cycle_running: bool = False
        self._cycle_pending: bool = False
        # 静默窗口定时器：每条新消息重置，N 秒无新消息后兜底触发 LLM
        self._quiet_timer: Optional[threading.Timer] = None
        self._quiet_timer_lock = threading.Lock()

    def warmup(self):
        """启动预热。"""
        self.napcat.warmup()
        self.self_qq = str(self.napcat.self_info.get("user_id", ""))
        self.self_nickname = self.napcat.self_info.get("nickname", "")
        self.trigger_evaluator = TriggerEvaluator(
            self.config, self.history, self.self_qq, self.config.persona.name,
            affinity_manager=self.affinity,
        )
        # 探测 AI 语音 character 是否可用（不阻塞启动）
        self._probe_ai_voice_character()
        logger.info(f"预热完成，机器人 {self.self_nickname}({self.self_qq})")

    def _probe_ai_voice_character(self):
        """探测配置的 AI 语音 character 是否在可用列表中。

        失败不阻塞启动，仅记 WARNING（语音调用时会触发 fallback_to_text）。
        """
        character = self.config.voice.ai_record_character
        try:
            characters = self.napcat.get_ai_characters()
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

    def on_group_message(self, msg: dict):
        """处理收到的群消息（接收线程，无 _cycle_lock）。

        职责：
        1. 入 fast_buffer（HistoryManager 内 _buffer_lock 保护，持锁极短）
        2. 评分（只读配置/亲密度，无锁）
        3. 触发决策：
           - 硬因子（@/提问）且距上次回复 ≥ HARD_COOLDOWN_SECONDS → 立即尝试触发
           - 其他 → 只入 buffer，由静默窗口兜底
        4. 无论如何重置静默定时器（每条新消息都推迟兜底触发）
        """
        try:
            # 防御性群过滤
            msg_group_id = str(msg.get("group_id", ""))
            if msg_group_id and msg_group_id != self.config.napcat.group_id:
                logger.debug(f"on_group_message 丢弃非目标群消息：{msg_group_id}")
                return

            sender_qq = str(msg.get("user_id", ""))
            sender_nick = self.napcat.get_nickname(sender_qq)
            content = msg.get("raw_message", "") or _extract_text_from_msg(msg)
            msg_id = str(msg.get("message_id", ""))

            # 语音消息：先用占位入历史（不阻塞），独立线程延迟重试转写后回填
            # NapCat 收到语音需先从腾讯下载 amr 文件，立即调用 fetch_ptt_text 会因文件未就绪而失败
            has_voice = self._has_voice_segment(msg)
            if has_voice:
                content = (content + (" " if content else "") + "[语音消息]").strip()

            # 1. 入 fast_buffer（无 _cycle_lock，仅 HistoryManager 内 _buffer_lock）
            self.history.append_group_message(sender_qq, sender_nick, content, msg_id)

            # 语音转写：异步延迟重试，成功后回填历史 content
            if has_voice and msg_id:
                threading.Thread(target=self._transcribe_voice_async, args=(msg_id,), daemon=True).start()

            # 2. 评分（接收线程，只读）
            score, soft_factors = self.trigger_evaluator.evaluate(msg)
            is_hard = self._is_hard_trigger(msg, content)
            logger.debug(f"消息评分={score} hard={is_hard} soft_factors={[f.name for f in soft_factors]}")

            # 3. 触发决策
            if is_hard:
                self._try_trigger_immediate(hard=True)
            elif self.trigger_evaluator.should_peek(score):
                self._try_trigger_immediate(hard=False, soft_factors=soft_factors)
            # 低分消息不立即触发，等静默窗口兜底

            # 4. 重置静默定时器（无论硬软，新消息都推迟兜底触发）
            self._reschedule_quiet_trigger()

        except Exception as e:
            logger.error(f"处理消息异常: {e}", exc_info=True)

    def _has_voice_segment(self, msg: dict) -> bool:
        """消息是否含语音段（record）。"""
        return any(seg.get("type") == "record" for seg in msg.get("message", []))

    def _transcribe_voice_async(self, msg_id: str):
        """异步转写语音：延迟重试，成功后回填历史 content。

        NapCat 收到语音消息后需先从腾讯下载 amr 文件到本地，立即调用 fetch_ptt_text
        会因文件未就绪而失败（retcode=200）。策略：延迟 2s 首次尝试，失败再等 3s 重试 1 次。
        成功则把历史中的 [语音消息] 占位回填为 [语音] 转写文字。
        """
        delays = [3.0, 1.0]  # 首次延迟 3s，重试间隔 1s
        for attempt, delay in enumerate(delays, 1):
            time.sleep(delay)
            try:
                text = self.napcat.fetch_ptt_text(msg_id)
                if text:
                    new_content = f"[语音] {text}"
                    updated = self.history.update_group_message_content(msg_id, new_content)
                    if updated:
                        logger.info(f"语音转文字成功并回填: {text[:50]}（第{attempt}次尝试）")
                    else:
                        logger.info(f"语音转文字成功但消息已被消费（msg_id={msg_id}）: {text[:50]}")
                    return
                logger.debug(f"语音转文字第{attempt}次返回空（msg_id={msg_id}）")
            except Exception as e:
                logger.error(f"语音转文字第{attempt}次异常（msg_id={msg_id}）: {e}")
        logger.warning(f"语音转文字最终失败，保留占位（msg_id={msg_id}）")

    def _is_hard_trigger(self, msg: dict, content: str) -> bool:
        """判断是否硬因子触发（@bot 或提问语气）。"""
        if self.trigger_evaluator._check_at_me(msg):
            return True
        if self.trigger_evaluator._check_question_to_me(content):
            return True
        return False

    def _try_enter_cycle(self) -> bool:
        """尝试进入 LLM cycle（B2 方案）。

        用 _cycle_running 标志保证 cycle 串行，代替 _llm_lock 覆盖全流程。
        _cycle_lock 只保护"检查+设置"（持锁极短），LLM 调用和发送不持锁。

        Returns:
            True: 成功进入 cycle（调用方负责执行 _run_llm_cycle + _exit_cycle）
            False: 已有 cycle 在跑，已注册 _cycle_pending 重试（调用方直接返回）
        """
        with self._cycle_lock:
            if self._cycle_running:
                self._cycle_pending = True
                return False
            self._cycle_running = True
            return True

    def _exit_cycle(self):
        """退出 LLM cycle，清除运行标志。"""
        with self._cycle_lock:
            self._cycle_running = False

    def _consume_pending_trigger(self) -> bool:
        """检查并消费重试标志（_run_llm_cycle while 循环末尾调用）。

        Returns:
            True: 有触发在 cycle 期间撞上，需要重跑
            False: 无重试，退出循环
        """
        with self._cycle_lock:
            if self._cycle_pending:
                self._cycle_pending = False
                return True
            return False

    def _try_trigger_immediate(self, hard: bool = False, soft_factors=None):
        """立即尝试触发 LLM（硬因子或软因子路径）。

        冷却检查：距上次回复 < cooldown 则跳过（让静默窗口兜底）。
        cycle 串行检查：撞 _cycle_running 则注册重试，cycle 完成后立即重跑。

        Args:
            hard: True=硬因子触发（@/提问），False=软因子触发（评分过阈值）
            soft_factors: 软因子触发时传入命中的软因子列表，供归因使用；
                         硬因子触发传 None，归因跳过。
        """
        now = time.time()
        elapsed = now - self.last_reply_time
        cooldown = HARD_COOLDOWN_SECONDS if hard else SOFT_COOLDOWN_MIN
        trigger_label = "硬因子" if hard else "软因子"
        if elapsed < cooldown:
            logger.debug(f"{trigger_label}触发但冷却中（elapsed={elapsed:.1f}s < {cooldown}s），等静默窗口兜底")
            return

        if not self._try_enter_cycle():
            logger.debug(f"{trigger_label}触发但 cycle 在跑，已注册 _cycle_pending（cycle 完成后按回复/silent 分别处理）")
            return

        try:
            self._run_llm_cycle(soft_factors=soft_factors, is_active=False)
        finally:
            self._exit_cycle()

    def _reschedule_quiet_trigger(self, delay: float = QUIET_WINDOW_SECONDS):
        """重置静默窗口定时器：N 秒后若仍无新消息则兜底触发。

        每条新消息都调用此方法，cancel 旧定时器并重设。
        """
        with self._quiet_timer_lock:
            if self._quiet_timer is not None:
                self._quiet_timer.cancel()
            timer = threading.Timer(delay, self._on_quiet_timeout)
            timer.daemon = True
            timer.start()
            self._quiet_timer = timer

    def _on_quiet_timeout(self):
        """静默窗口到期：兜底触发 LLM。

        冷却检查：距上次回复 < SOFT_COOLDOWN_MIN 则重调度到冷却到期。
        cycle 串行检查：撞 _cycle_running 则注册重试，cycle 完成后立即重跑。
        """
        try:
            now = time.time()
            elapsed = now - self.last_reply_time
            if elapsed < SOFT_COOLDOWN_MIN:
                # 还在软冷却内，重调度到冷却到期
                remaining = SOFT_COOLDOWN_MIN - elapsed
                logger.debug(f"静默窗口到期但软冷却中（elapsed={elapsed:.1f}s），{remaining:.1f}s 后再触发")
                self._reschedule_quiet_trigger(delay=remaining)
                return

            if not self._try_enter_cycle():
                logger.debug("静默窗口到期但 cycle 在跑，已注册完成后重试")
                return

            try:
                self._run_llm_cycle(soft_factors=None, is_active=False)
            finally:
                self._exit_cycle()
        except Exception as e:
            logger.error(f"静默窗口兜底触发异常: {e}", exc_info=True)

    def on_active_trigger(self):
        """主动触发回调（由调度器调用）。

        cycle 串行检查：撞 _cycle_running 则注册重试（重跑时 is_active 丢失，
        语义上等同于"群里正在活跃对话，不需要主动开口"）。
        """
        try:
            logger.info("主动触发：执行 LLM 调用")
            if not self._try_enter_cycle():
                logger.debug("主动触发但 cycle 在跑，已注册完成后重试")
                return

            try:
                self._run_llm_cycle(soft_factors=None, is_active=True)
            finally:
                self._exit_cycle()
        except Exception as e:
            logger.error(f"主动触发异常: {e}", exc_info=True)

    def _run_llm_cycle(self, soft_factors, is_active: bool):
        """LLM 工作循环（B2 方案：调用方已通过 _try_enter_cycle 获取运行权）。

        流程（while 循环，处理撞 cycle 重试）：
        1. drain fast_buffer → pending（_buffer_lock 保护，持锁极短）
        2. 检查延迟回复到期 → 加入 pending
        3. 若 pending 空（且非主动触发）→ 检查重试或退出
        4. 构建 user_content（创建 _rendered_pending_snapshot 快照，供 reply/react 引用）
        5. 调用 LLM（不持锁，_cycle_running 保证串行）
        6. 落地 user/assistant turn（pending 清空，append_turn）
        7. 发送 + 归因（不持锁，发送期间群消息和 bot 回复都进 fast_buffer）
        8. 检查 _cycle_pending：有则继续循环（重跑不传 soft_factors，归因跳过），无则退出

        注：LLM 调用和发送不持任何锁，接收线程写 fast_buffer 不受影响。
        pending/messages/affinity/attribution 只在本线程访问，无竞争。
        """
        while True:
            # 1. drain fast_buffer
            drained = self.history.drain_buffer_to_pending()
            if drained > 0:
                logger.debug(f"drain {drained} 条消息到 pending")

            # 2. 延迟回复到期
            due_count = self.history.pop_due_delayed_into_pending()
            if due_count > 0:
                logger.info(f"延迟回复到期，{due_count} 条消息已加入 pending")

            # 3. pending 空检查（主动触发允许空 pending，让 LLM 决定是否主动开口）
            if not self.history.pending_group_msgs and not is_active:
                logger.debug("pending 为空，跳过 LLM 调用")
                if self._consume_pending_trigger():
                    continue
                return

            # 4. 构建 user_content（创建快照供 reply/react 段引用）
            summary = self.history.get_summary()
            system_prompt = self.persona_renderer.render_system_prompt(summary)
            history_messages = self.history.get_messages_for_llm()
            member_list = self._build_member_list()
            pending_text = self.history.build_user_content()
            new_user_content = self.persona_renderer.render_user_content(
                pending_text, member_list, self.self_nickname, self.self_qq,
                is_active=is_active,
            )

            # 5. 调用 LLM（不持锁，_cycle_running 标志保证串行）
            raw_result = self.llm.chat(system_prompt, history_messages, new_user_content)
            if raw_result is None:
                logger.warning("LLM 调用失败，本轮跳过")
                # pending 留待下次触发再拼入（消息不丢失）
                if self._consume_pending_trigger():
                    continue
                return

            # 6. 解析 + 落地
            parsed = parse_and_validate(raw_result)
            logger.info(f"LLM 返回 action={parsed.action} thought={parsed.thought[:50]}"
                        f"{' reply_delay=' + str(parsed.reply_delay_minutes) + 'min' if parsed.reply_delay_minutes > 0 else ''}"
                        f"{' [主动触发]' if is_active else ''}")

            # 延迟回复处理：LLM 觉得"等会再回"，把这批消息存到 delayed_replies
            if parsed.reply_delay_minutes > 0 and parsed.action == "silent":
                self.history.stash_pending_as_delayed(parsed.reply_delay_minutes)
                if self._consume_pending_trigger():
                    continue
                return

            # 清空 pending（user_content 已在步骤 4 构建，不重新 build）
            self.history.consume_pending_into_user()
            self.history.append_turn(new_user_content, raw_result)

            # 7. 发送 + 归因（不持锁，发送期间群消息和 bot 回复都进 fast_buffer）
            self._handle_result(parsed, soft_factors)

            # 8. 检查重试：cycle 期间有新触发撞上则重跑（不传 soft_factors，归因跳过）
            #    - 若本轮 cycle 发了回复（last_reply_time 新近）：刚说过话，新消息攒着等静默窗口兜底
            #      （模拟真人"说完一轮先听一会"，不立即接话）
            #    - 若本轮 cycle 返回 silent（last_reply_time 较旧）：没说过话，新消息立即重跑
            #      （模拟真人"没开口时新消息值得看一眼"）
            soft_factors = None
            is_active = False
            if not self._consume_pending_trigger():
                return
            if time.time() - self.last_reply_time < SOFT_COOLDOWN_MIN:
                logger.debug("刚回完消息，新触发等静默窗口兜底（攒消息等下一轮）")
                return
            logger.debug("上一轮 silent，立即重跑 cycle 处理撞锁的新触发")
            # 继续循环

    def _handle_result(self, parsed, soft_factors):
        """处理 LLM 结果：执行动作 -> 写回历史 -> 亲密度 -> 归因。

        Args:
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
                self.attribution.update(soft_factors, "silent")
            self.affinity.apply_delta(parsed.affinity_delta)
            return

        if parsed.action == "react":
            # 预留接口
            msg_id = self.history.get_msg_id_by_index(parsed.react_target_msg_index)
            self.emoji_reactor.react(self.config.napcat.group_id, msg_id, parsed.react_emoji_id)
            if soft_factors is not None:
                self.attribution.update(soft_factors, "react")
            self.affinity.apply_delta(parsed.affinity_delta)
            return

        # reply / multi_reply
        # _send_messages 内部每条发送完成后会逐条 append 到 fast_buffer（is_bot=True），
        # 与此期间群成员的新消息按真实 append 顺序混合，下一轮 drain 时进入 pending。
        self._send_messages(parsed.messages)
        self.last_reply_time = time.time()

        # 亲密度更新
        self.affinity.apply_delta(parsed.affinity_delta)

        # 归因更新（主动触发时 soft_factors=None，跳过）
        if soft_factors is not None:
            self.attribution.update(soft_factors, parsed.action)

    def _send_messages(self, messages: list):
        """发送消息列表，带间隔。

        每条消息发送完成后立即 append 到 fast_buffer（is_bot=True），
        与此期间接收线程写入的群消息按真实 append 顺序混合，
        保证下一轮 LLM 看到的发言顺序 = 真实群聊发言顺序。
        multi_reply 逐条 append，中间间隔期间群成员插话会自然夹在 bot 消息之间。
        """
        segments_list = self.message_sender.build_segments(messages, self.history)
        for i, segs in enumerate(segments_list):
            # 处理特殊段
            handled = False
            for seg in segs:
                if seg.get("type") == "forward":
                    data = seg.get("data", {})
                    self.napcat.send_group_forward_msg(data.get("messages", []), data.get("title", ""))
                    handled = True
                    break
                if seg.get("type") == "image":
                    try:
                        self.image_sender.send(self.config.napcat.group_id, seg.get("data", {}))
                    except NotImplementedError:
                        pass
                    handled = True
                    break
                if seg.get("type") == "voice":
                    data = seg.get("data", {})
                    channel = data.get("channel", "ai_record")
                    if channel == "ai_record":
                        self.ai_voice_sender.send(self.config.napcat.group_id, data)
                    elif channel == "local_file":
                        self.local_voice_sender.send(self.config.napcat.group_id, data)
                    handled = True
                    break
            if handled:
                # 特殊段也记入 fast_buffer（语音/图片/转发都算一条 bot 发言）
                self._append_bot_reply_to_buffer(messages[i])
                continue

            # 普通消息段
            normal_segs = [s for s in segs if s.get("type") not in ("forward", "image", "voice")]
            if normal_segs:
                self.message_sender.send_group_message(self.config.napcat.group_id, normal_segs)

            # 逐条 append 到 fast_buffer（与群消息按真实时间顺序混合）
            self._append_bot_reply_to_buffer(messages[i])

            # 多条消息间隔
            if i < len(segments_list) - 1:
                time.sleep(random.uniform(0.8, 2.5))

    def _append_bot_reply_to_buffer(self, msg):
        """把单条 bot 发言追加到 fast_buffer（is_bot=True）。

        在 _send_messages 每条发送完成后调用，确保发言顺序与真实群聊一致。
        文本摘要复用 _msg_to_text，特殊段（image/voice/forward）也能得到合理摘要。
        """
        msg_text = _msg_to_text(msg)
        self.history.append_group_message(
            self.self_qq, self.self_nickname, msg_text, "", is_bot=True
        )

    def _build_member_list(self) -> list:
        """构建传给 LLM 的群成员列表（含亲密度）。"""
        result = []
        for qq, info in self.napcat.member_cache.items():
            result.append({
                "qq": qq,
                "nickname": info.get("card") or info.get("nickname") or qq,
                "role": info.get("role", "member"),
                "affinity": self.affinity.get(qq),
            })
        return result

    def run(self, webhook_host: str = "0.0.0.0", webhook_port: int = 8081):
        """启动机器人。"""
        self.warmup()
        server = NapCatWebhookServer(
            webhook_host, webhook_port, self.on_group_message,
            target_group_id=self.config.napcat.group_id,
        )
        # 启动主动触发调度器
        if self.config.active_trigger and self.config.active_trigger.enabled:
            self.scheduler.start(self.on_active_trigger)
            logger.info(f"主动触发调度器已启动：{self.config.active_trigger.min_interval_minutes}-"
                        f"{self.config.active_trigger.max_interval_minutes} 分钟随机，"
                        f"深夜 {self.config.active_trigger.night_start_hour}:00-"
                        f"{self.config.active_trigger.night_end_hour}:00 禁用")
        else:
            logger.info("主动触发调度器已禁用")
        logger.info(f"机器人启动（仅监听群 {self.config.napcat.group_id}）")
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


if __name__ == "__main__":
    bot = Bot()
    bot.run()
