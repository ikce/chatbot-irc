from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_SECRET: str = "change-me"
    LOG_LEVEL: str = "INFO"
    BOOTSTRAP_FROM_ENV: bool = True

    DATA_DIR: str = "/app/data"

    ZNC_HOST: str = "znc.example.tld"
    ZNC_PORT: int = 6697
    ZNC_TLS: bool = True
    ZNC_USER: str = ""
    ZNC_PASS: str = ""
    ZNC_NICK: str = "bibi"
    ZNC_IDENT: str = "bibi"
    ZNC_REALNAME: str = "Bibi le chatter bot"
    ZNC_CHANNELS: str = "#lab"

    # Endpoint OpenAI-compatible d'OpenWebUI (typiquement https://owui.example.tld)
    # Le code append /api/chat/completions lui-même.
    LLM_URL: str = "http://openwebui.local:3000"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "hf.co/cognitivecomputations/dolphin-2.9.3-mistral-nemo-12b-gguf:Q6_K"
    LLM_MAX_TOKENS: int = 400
    LLM_TEMPERATURE: float = 0.7
    LLM_SYSTEM_PROMPT: str = (
        "Tu es un chatter bot IRC concis et amical. "
        "Réponds en français en 1 à 3 phrases maximum."
    )
    WEB_SEARCH_ENABLED: bool = False

    THROTTLE_GLOBAL_SEC: float = 3.0
    THROTTLE_PER_NICK_SEC: float = 10.0


settings = Settings()
