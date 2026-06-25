"""配置加载模块。"""
from dataclasses import dataclass, field
from typing import Any
import yaml


@dataclass
class NapCatConfig:
    base_url: str
    group_id: str


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
    attribution_step: float
    cold_start_rounds: int


@dataclass
class Config:
    napcat: NapCatConfig
    llm: LLMConfig
    persona: PersonaConfig
    voice: VoiceConfig
    trigger: TriggerConfig
    trigger_factors: dict      # 软因子 base_value
    trigger_hard_factors: dict # 硬因子 base_value（不参与归因）


def load_config(path: str = "config.yaml") -> Config:
    """从 YAML 文件加载配置。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return Config(
        napcat=NapCatConfig(**raw["napcat"]),
        llm=LLMConfig(**raw["llm"]),
        persona=PersonaConfig(**raw["persona"]),
        voice=VoiceConfig(**raw["voice"]),
        trigger=TriggerConfig(**raw["trigger"]),
        trigger_factors=raw.get("trigger_factors", {}),
        trigger_hard_factors=raw.get("trigger_hard_factors", {}),
    )
