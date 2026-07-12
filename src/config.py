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
    )
