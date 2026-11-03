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
    forbidden: list


@dataclass
class VoiceConfig:
    ai_record_character: str
    fallback_to_text: bool


@dataclass
class TriggerConfig:
    peek_threshold: int
    silent_buffer_limit: int
    history_limit: int
    history_keep_recent: int
    history_keep_mid: int = 0   # 中期摘要轮数（方案A分层压缩，0=禁用分层走旧逻辑）
    attribution_step: float = 0.1
    cold_start_rounds: int = 20


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
class PrefixConfig:
    """对话前缀续写配置（DeepSeek Beta）。

    用户在消息末尾追加 `command 开头内容`，框架把解析后的行指令作为
    assistant 前缀发给 Beta 接口，让模型从指定位置继续生成。
    """
    enabled: bool = False
    command: str = "/pf"
    beta_api_url: str = "https://api.deepseek.com/beta/chat/completions"


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
    prefix: PrefixConfig = None                  # 对话前缀续写配置


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

    prefix_raw = raw.get("prefix", {})
    prefix = PrefixConfig(
        enabled=prefix_raw.get("enabled", False),
        command=prefix_raw.get("command", "/pf"),
        beta_api_url=prefix_raw.get("beta_api_url", "https://api.deepseek.com/beta/chat/completions"),
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
        prefix=prefix,
    )
