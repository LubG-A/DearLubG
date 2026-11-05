"""主动触发调度器（群聊 / 私聊共用）。

每个 chat（群或私聊对象）持有一个 ActiveScheduler 实例，独立 Timer，
独立状态文件（群聊 state/{group_id}/scheduler.json，私聊 state/private/{qq}/scheduler.json）。
间隔沿用随机 min-max 逻辑，深夜禁用主动触发。

状态持久化：跨重启保留 last_active_check_time 和 next_active_check_time。
"""
import json
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .config import ActiveTriggerConfig, Config
from .utils.logger import get_logger

logger = get_logger("scheduler")


class ActiveScheduler:
    """per-chat 主动触发调度器。"""

    def __init__(self, config: Config, group_id: str, state_dir: str = "state",
                 active_config: Optional[ActiveTriggerConfig] = None, label: str = ""):
        self.config = config
        self.group_id = group_id
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "scheduler.json"
        # 主动触发参数：群聊默认用全局 active_trigger，私聊可传自己的配置
        self.active_config = active_config or config.active_trigger
        self.label = label or f"群 {group_id}"

        # 状态
        self.last_active_check_time: Optional[datetime] = None
        self.next_active_check_time: Optional[datetime] = None

        # 定时器线程
        self._timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()

        # 回调（主动触发时调用，签名 (group_id) -> None）
        self._callback: Optional[Callable[[str], None]] = None

        self._load()

    # ---------- 状态持久化 ----------
    def _load(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if data.get("last_active_check_time"):
                    self.last_active_check_time = datetime.fromisoformat(data["last_active_check_time"])
                if data.get("next_active_check_time"):
                    self.next_active_check_time = datetime.fromisoformat(data["next_active_check_time"])
            except Exception as e:
                logger.warning(f"加载调度器状态失败: {e}")

    def _save(self):
        data = {
            "last_active_check_time": self.last_active_check_time.isoformat() if self.last_active_check_time else None,
            "next_active_check_time": self.next_active_check_time.isoformat() if self.next_active_check_time else None,
        }
        self.state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 定时器管理 ----------
    def _generate_next_check_time(self) -> datetime:
        """生成下次主动触发时间。

        在 [min, max] 区间内随机取分钟数，跳过深夜时段。
        """
        cfg = self.active_config
        interval = random.randint(cfg.min_interval_minutes, cfg.max_interval_minutes)
        now = datetime.now()
        next_time = now + timedelta(minutes=interval)

        # 跳过深夜时段（23:00-3:00）
        # 如果下次时间落在深夜，顺延到次日 3:00 之后
        cfg_night_start = cfg.night_start_hour
        cfg_night_end = cfg.night_end_hour
        hour = next_time.hour
        if cfg_night_start > cfg_night_end:
            # 跨天（如 23-3）
            in_night = hour >= cfg_night_start or hour < cfg_night_end
        else:
            # 不跨天（如 0-6）
            in_night = cfg_night_end <= hour < cfg_night_start

        if in_night:
            # 顺延到次日 night_end_hour
            next_date = next_time.date() + timedelta(days=1)
            next_time = datetime.combine(next_date, datetime.min.time()).replace(
                hour=cfg_night_end, minute=random.randint(0, 59), second=random.randint(0, 59)
            )

        return next_time

    def _schedule_next(self):
        """安排下一次主动触发。"""
        if self._stop_event.is_set():
            return

        if not self.active_config.enabled:
            logger.debug(f"{self.label} 主动触发已禁用")
            return

        self.next_active_check_time = self._generate_next_check_time()
        self._save()

        # 计算等待秒数
        now = datetime.now()
        wait_seconds = (self.next_active_check_time - now).total_seconds()
        if wait_seconds <= 0:
            # 已过期，立即触发（加小延迟避免密集调用）
            wait_seconds = 5

        logger.info(f"{self.label} 下次主动触发：{self.next_active_check_time.strftime('%Y-%m-%d %H:%M:%S')}（{wait_seconds/60:.1f} 分钟后）")

        # 取消旧定时器
        if self._timer:
            self._timer.cancel()

        # 安排新定时器
        self._timer = threading.Timer(wait_seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        """定时器触发回调。"""
        if self._stop_event.is_set():
            return

        # 再次检查是否在深夜（防止时间漂移）
        now = datetime.now()
        cfg = self.active_config
        hour = now.hour
        if cfg.night_start_hour > cfg.night_end_hour:
            in_night = hour >= cfg.night_start_hour or hour < cfg.night_end_hour
        else:
            in_night = cfg.night_end_hour <= hour < cfg.night_start_hour

        if in_night:
            logger.info(f"{self.label} 当前为深夜，跳过本次主动触发，重新调度")
            self._schedule_next()
            return

        logger.info(f"{self.label} 主动触发定时器到期，执行回调")
        self.last_active_check_time = now
        self._save()

        try:
            if self._callback:
                self._callback(self.group_id)
        except Exception as e:
            logger.error(f"{self.label} 主动触发回调异常: {e}", exc_info=True)
        finally:
            # 安排下一次
            self._schedule_next()

    # ---------- 公共接口 ----------
    def start(self, callback: Callable[[str], None], group_id: str):
        """启动调度器。

        Args:
            callback: 主动触发时调用的回调，签名 (group_id) -> None
            group_id: 本调度器所属的群 ID
        """
        self._callback = callback
        self.group_id = group_id
        self._stop_event.clear()

        # 如果有保存的 next_active_check_time 且未过期，按原计划
        # 否则生成新的
        now = datetime.now()
        if self.next_active_check_time and self.next_active_check_time > now:
            logger.info(f"{self.label} 恢复调度器计划：{self.next_active_check_time.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            self.next_active_check_time = self._generate_next_check_time()
            self._save()
            logger.info(f"{self.label} 生成新调度计划：{self.next_active_check_time.strftime('%Y-%m-%d %H:%M:%S')}")

        wait_seconds = (self.next_active_check_time - now).total_seconds()
        if wait_seconds <= 0:
            wait_seconds = 5

        self._timer = threading.Timer(wait_seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def stop(self):
        """停止调度器。"""
        self._stop_event.set()
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info(f"{self.label} 调度器已停止")

    def is_active_check_time(self) -> bool:
        """当前是否为主动触发状态（供 main.py 判断）。"""
        # 通过标志位判断，由 callback 设置
        return False

    def get_status(self) -> dict:
        """获取调度器状态。"""
        return {
            "last_active_check_time": self.last_active_check_time.isoformat() if self.last_active_check_time else None,
            "next_active_check_time": self.next_active_check_time.isoformat() if self.next_active_check_time else None,
            "enabled": self.active_config.enabled,
        }


def _parse_times(times: list) -> list[tuple[int, int]]:
    """解析 ["12:00", "0:30"] 为 [(h, m), ...]，非法项忽略。"""
    if isinstance(times, str):
        times = [times]
    parsed = []
    for item in times or []:
        text = str(item).strip()
        if not text or ":" not in text:
            logger.warning(f"忽略非法记忆整理时刻: {item}")
            continue
        hour_s, _, minute_s = text.partition(":")
        try:
            hour, minute = int(hour_s), int(minute_s)
        except ValueError:
            logger.warning(f"忽略非法记忆整理时刻: {item}")
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            logger.warning(f"忽略越界记忆整理时刻: {item}")
            continue
        parsed.append((hour, minute))
    return sorted(set(parsed))


class FixedTimeScheduler:
    """每天固定时刻触发的轻量调度器（记忆整理用）。

    与 ActiveScheduler 的区别：
    - 按固定时刻（如 12:00 / 00:00）触发，不做随机间隔与深夜跳过；
    - 不持久化状态，重启后从当前时间重新计算下一个时刻；
    - 回调无参数，调度器实例本身属于某个 chat。
    """

    def __init__(self, times: list, callback: Callable[[], None], label: str = ""):
        self.times = _parse_times(times)
        self._callback = callback
        self.label = label
        self._timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()

    def _next_time(self) -> datetime:
        now = datetime.now()
        for hour, minute in self.times:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                return candidate
        first_hour, first_minute = self.times[0]
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=first_hour, minute=first_minute, second=0, microsecond=0)

    def _schedule_next(self):
        if self._stop_event.is_set() or not self.times:
            return
        next_time = self._next_time()
        wait_seconds = max(5.0, (next_time - datetime.now()).total_seconds())
        logger.info(f"{self.label} 下次记忆整理：{next_time.strftime('%Y-%m-%d %H:%M:%S')}"
                    f"（{wait_seconds/60:.1f} 分钟后）")
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(wait_seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        if self._stop_event.is_set():
            return
        logger.info(f"{self.label} 记忆整理定时器到期")
        try:
            self._callback()
        except Exception as e:
            logger.error(f"{self.label} 记忆整理回调异常: {e}", exc_info=True)
        finally:
            self._schedule_next()

    def start(self):
        if not self.times:
            logger.info(f"{self.label} 未配置有效时刻，记忆整理调度器不启动")
            return
        self._stop_event.clear()
        self._schedule_next()

    def stop(self):
        self._stop_event.set()
        if self._timer:
            self._timer.cancel()
            self._timer = None
