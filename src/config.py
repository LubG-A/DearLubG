"""配置加载模块。"""
from dataclasses import dataclass, field
from typing import Any
import yaml


@dataclass
class NapCatConfig:
    base_url: str
    group_ids: list = field(default_factory=list)  # 群列表（至少一个）


@dataclass
class LLMConfig:
    api_url: str
    api_key: str
    model: str


@dataclass
class PersonaConfig:
    name: str
    gender: str
    age: str
    location: str
    job: str
    traits: list
    interests: list
    catchphrases: list
    style: str


@dataclass
class VoiceConfig:
    ai_record_character: str
    fallback_to_text: bool


@dataclass
class TriggerConfig:
    peek_threshold: int
    silent_buffer_limit: int
    history_limit: int          # 总轮数达 history_limit/2 时触发记忆整理，整理成功后才裁剪原文
    history_keep_recent: int    # 整理后保留的近期原文轮数
    recent_pack_turns: int = 6  # P006：最近 N 轮对话块留在内存队列，打包进当前 user content
    attribution_step: float = 0.1
    cold_start_rounds: int = 20


@dataclass
class MemoryConfig:
    """记忆系统配置（长期记忆 + 近期日记 + 短期备忘）。

    - enabled=False 时完全关闭记忆功能（不写文件、不注入 prompt、不调度整理）。
    - 长期记忆（core）放 system prompt，仅在记忆整理时变化；
      近期日记（journal）与备忘（memo）放 user content，避免破坏前缀缓存。
    """
    enabled: bool = True
    recent_journal_hours: int = 48          # 注入 user content 的近期日记时间窗
    recent_journal_max_entries: int = 20    # 注入 user content 的近期日记最大条数
    journal_entry_max_chars: int = 120      # 单条日记最大字数
    journal_keep_hours: int = 72            # 整理后保留日记的时间窗
    journal_keep_max_entries: int = 60      # 整理后保留日记的最大条数
    core_max_chars: int = 1200              # 长期记忆最大字数
    consolidate_times: list = field(default_factory=lambda: ["12:00", "00:00"])  # 每日记忆整理时刻
    memo_enabled: bool = True               # 短期备忘开关
    memo_default_ttl_hours: int = 72        # 备忘默认过期时间（小时）
    memo_max_items: int = 10                # 备忘最大条数


@dataclass
class ActiveTriggerConfig:
    """主动触发配置。"""
    enabled: bool = True
    min_interval_minutes: int = 120   # 主动触发最小间隔（分钟）
    max_interval_minutes: int = 360   # 主动触发最大间隔（分钟）
    night_start_hour: int = 23        # 深夜起始小时（禁用主动触发）
    night_end_hour: int = 3           # 深夜结束小时


@dataclass
class MediaDownloadConfig:
    """媒体下载配置：网络资源先下载到本地再发送，规避 NapCat 10s 超时。"""
    timeout: int = 30                 # 普通资源（图片/语音）下载超时（秒）
    video_timeout: int = 60           # 视频资源下载超时（秒）
    temp_dir: str = "media/downloaded"  # 临时文件目录


@dataclass
class PrivateConfig:
    """私聊配置。

    私聊是群聊的减法：不做评分/归因/direct 状态；可选主动触发（偶尔主动找对方）。
    """
    enabled: bool = True
    allow_list: list = field(default_factory=list)  # 允许私聊的 QQ 白名单
    cooldown_seconds: int = 3                        # 回复后的冷却（秒）
    active_trigger: ActiveTriggerConfig = None      # 私聊主动触发（缺省关闭）


@dataclass
class VisionConfig:
    """识图配置（DeepSeek 多模态图片输入）。

    DeepSeek 支持 JPEG/PNG/GIF/WebP，图片只能放在 user 消息中。
    本框架将当前批次图片转成 base64 data URL 后插入 user content。
    """
    enabled: bool = True
    max_images_per_turn: int = 6      # 单轮最多送几张图（DeepSeek 上限 600，这里控成本）
    max_side: int = 1280              # 图片最长边，超过则等比缩小（DeepSeek 会自动缩到约 1300）
    detail: str = "auto"              # low / high / original / auto


@dataclass
class Config:
    napcat: NapCatConfig
    llm: LLMConfig
    persona: PersonaConfig
    voice: VoiceConfig
    trigger: TriggerConfig
    trigger_factors: dict      # 软因子 base_value
    trigger_hard_factors: dict # 硬因子 base_value（不参与归因）
    active_trigger: ActiveTriggerConfig = None  # 主动触发配置
    media_download: MediaDownloadConfig = None  # 媒体下载配置
    private: PrivateConfig = None                # 私聊配置
    vision: VisionConfig = None                  # 识图配置
    memory: MemoryConfig = None                  # 记忆系统配置


def load_config(path: str = "config.yaml") -> Config:
    """从 YAML 文件加载配置。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    active_raw = raw.get("active_trigger", {})
    active = ActiveTriggerConfig(
        enabled=active_raw.get("enabled", True),
        min_interval_minutes=active_raw.get("min_interval_minutes", 120),
        max_interval_minutes=active_raw.get("max_interval_minutes", 360),
        night_start_hour=active_raw.get("night_start_hour", 23),
        night_end_hour=active_raw.get("night_end_hour", 3),
    )

    md_raw = raw.get("media_download", {})
    media_download = MediaDownloadConfig(
        timeout=md_raw.get("timeout", 30),
        video_timeout=md_raw.get("video_timeout", 60),
        temp_dir=md_raw.get("temp_dir", "media/downloaded"),
    )

    private_raw = raw.get("private", {})
    private_active_raw = private_raw.get("active_trigger", {})
    private_active = ActiveTriggerConfig(
        enabled=private_active_raw.get("enabled", False),
        min_interval_minutes=private_active_raw.get("min_interval_minutes", 120),
        max_interval_minutes=private_active_raw.get("max_interval_minutes", 360),
        night_start_hour=private_active_raw.get("night_start_hour", 23),
        night_end_hour=private_active_raw.get("night_end_hour", 3),
    )
    private = PrivateConfig(
        enabled=private_raw.get("enabled", True),
        allow_list=[str(q) for q in private_raw.get("allow_list", [])],
        cooldown_seconds=private_raw.get("cooldown_seconds", 3),
        active_trigger=private_active,
    )

    vision_raw = raw.get("vision", {})
    detail = vision_raw.get("detail", "auto")
    if detail not in ("low", "high", "original", "auto"):
        detail = "auto"
    vision = VisionConfig(
        enabled=vision_raw.get("enabled", True),
        max_images_per_turn=vision_raw.get("max_images_per_turn", 6),
        max_side=vision_raw.get("max_side", 1280),
        detail=detail,
    )

    memory_raw = raw.get("memory", {})
    memory = MemoryConfig(
        enabled=memory_raw.get("enabled", True),
        recent_journal_hours=memory_raw.get("recent_journal_hours", 48),
        recent_journal_max_entries=memory_raw.get("recent_journal_max_entries", 20),
        journal_entry_max_chars=memory_raw.get("journal_entry_max_chars", 120),
        journal_keep_hours=memory_raw.get("journal_keep_hours", 72),
        journal_keep_max_entries=memory_raw.get("journal_keep_max_entries", 60),
        core_max_chars=memory_raw.get("core_max_chars", 1200),
        consolidate_times=memory_raw.get("consolidate_times", ["12:00", "00:00"]),
        memo_enabled=memory_raw.get("memo_enabled", True),
        memo_default_ttl_hours=memory_raw.get("memo_default_ttl_hours", 72),
        memo_max_items=memory_raw.get("memo_max_items", 10),
    )

    return Config(
        napcat=NapCatConfig(
            base_url=raw["napcat"]["base_url"],
            group_ids=raw["napcat"].get("group_ids", []),
        ),
        llm=LLMConfig(**raw["llm"]),
        persona=PersonaConfig(**raw["persona"]),
        voice=VoiceConfig(**raw["voice"]),
        trigger=TriggerConfig(**raw["trigger"]),
        trigger_factors=raw.get("trigger_factors", {}),
        trigger_hard_factors=raw.get("trigger_hard_factors", {}),
        active_trigger=active,
        media_download=media_download,
        private=private,
        vision=vision,
        memory=memory,
    )
