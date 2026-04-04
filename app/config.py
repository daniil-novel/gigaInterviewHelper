from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'gigaInterviewHelper'
    app_base_url: str = 'http://127.0.0.1:8000'
    database_url: str = 'sqlite:///./app.db'
    openrouter_api_key: str = ''
    openrouter_model: str = 'google/gemini-3.1-flash-lite-preview'
    target_email_senders_raw: str = Field(default='hrplatform@sberbank.ru', alias='TARGET_EMAIL_SENDERS')
    target_email_subject_keywords_raw: str = Field(default='ai-интервью,interview,приглашение', alias='TARGET_EMAIL_SUBJECT_KEYWORDS')
    max_answer_sentences: int = 4
    max_answer_chars: int = 500

    @property
    def target_email_senders(self) -> List[str]:
        return [item.strip().lower() for item in self.target_email_senders_raw.split(',') if item.strip()]

    @property
    def target_email_subject_keywords(self) -> List[str]:
        return [item.strip().lower() for item in self.target_email_subject_keywords_raw.split(',') if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
