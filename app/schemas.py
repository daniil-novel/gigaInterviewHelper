from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EmailIngestPayload(BaseModel):
    source_email: str
    subject: str
    html_body: str = ''
    text_body: str = ''
    received_at: datetime | None = None


class EmailParseResult(BaseModel):
    matched: bool
    source_email: str
    subject: str
    vacancy_name: str = ''
    interview_url: str = ''
    received_at: datetime
    failure_reason: str = ''


class CandidateProfilePayload(BaseModel):
    full_name: str = ''
    current_role: str = ''
    experience_summary: str = ''
    skills: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    english_level: str = ''
    salary_expectation: str = ''
    work_format: str = ''
    notice_period: str = ''
    must_not_claim: list[str] = Field(default_factory=list)
    source_resume_name: str = ''
    raw_resume_text: str = ''
    parsing_notes: str = ''
    last_parsed_with_model: str = ''


class SessionCreatePayload(BaseModel):
    vacancy_name: str
    interview_url: str
    source_email: str = ''
    subject: str = ''


class QuestionCreatePayload(BaseModel):
    question: str
    regenerate: bool = False


class AnswerActionPayload(BaseModel):
    action: Literal['approve', 'skip', 'edit']
    final_answer: str = ''


class GenerateAnswerResponse(BaseModel):
    question_id: int
    draft_answer: str
    validation_status: str
    needs_review: bool
    warnings: list[str] = Field(default_factory=list)
    source: str = 'fallback'


class DashboardStats(BaseModel):
    sessions_total: int
    sessions_waiting_approval: int
    questions_total: int
    emails_total: int
    last_resume_update: datetime | None = None


class LogEventPayload(BaseModel):
    level: str = 'info'
    event_type: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class OpenRouterSettingsPayload(BaseModel):
    openrouter_api_key: str = ''
    openrouter_model: str = 'google/gemini-3.1-flash-lite-preview'
    pdf_engine: str = 'pdf-text'


class MailboxSettingsPayload(BaseModel):
    mail_provider: str = 'gmail_oauth'
    personal_email: str = ''
    gmail_oauth_client_id: str = ''
    gmail_oauth_client_secret: str = ''
    gmail_oauth_redirect_uri: str = ''
    imap_host: str = ''
    imap_port: int = 993
    imap_password: str = ''
    imap_folder: str = 'INBOX'
    mail_poll_enabled: bool = False
    mail_poll_interval_seconds: int = 60


class TelegramSettingsPayload(BaseModel):
    telegram_api_id: str = ''
    telegram_api_hash: str = ''
    telegram_phone_number: str = ''
    telegram_2fa_password: str = ''
    auto_send_telegram: bool = False


class TelegramCodeRequestPayload(BaseModel):
    telegram_api_id: str = ''
    telegram_api_hash: str = ''
    telegram_phone_number: str = ''


class TelegramCodeVerifyPayload(BaseModel):
    code: str
    telegram_2fa_password: str = ''
