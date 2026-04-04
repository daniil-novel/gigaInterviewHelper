from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CandidateProfile(Base, TimestampMixin):
    __tablename__ = 'candidate_profiles'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), default='')
    current_role: Mapped[str] = mapped_column(String(255), default='')
    experience_summary: Mapped[str] = mapped_column(Text, default='')
    skills: Mapped[list] = mapped_column(JSON, default=list)
    projects: Mapped[list] = mapped_column(JSON, default=list)
    education: Mapped[list] = mapped_column(JSON, default=list)
    achievements: Mapped[list] = mapped_column(JSON, default=list)
    english_level: Mapped[str] = mapped_column(String(100), default='')
    salary_expectation: Mapped[str] = mapped_column(String(100), default='')
    work_format: Mapped[str] = mapped_column(String(100), default='')
    notice_period: Mapped[str] = mapped_column(String(100), default='')
    must_not_claim: Mapped[list] = mapped_column(JSON, default=list)
    source_resume_name: Mapped[str] = mapped_column(String(255), default='')
    raw_resume_text: Mapped[str] = mapped_column(Text, default='')
    parsing_notes: Mapped[str] = mapped_column(Text, default='')
    last_parsed_with_model: Mapped[str] = mapped_column(String(255), default='')


class EmailLog(Base, TimestampMixin):
    __tablename__ = 'email_logs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(500))
    vacancy_name: Mapped[str] = mapped_column(String(255), default='')
    interview_url: Mapped[str] = mapped_column(String(1000), default='')
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    html_body: Mapped[str] = mapped_column(Text, default='')
    text_body: Mapped[str] = mapped_column(Text, default='')
    matched: Mapped[str] = mapped_column(String(20), default='no')
    failure_reason: Mapped[str] = mapped_column(String(255), default='')
    remote_message_id: Mapped[str] = mapped_column(String(255), default='')
    remote_uid: Mapped[str] = mapped_column(String(100), default='')


class InterviewSession(Base, TimestampMixin):
    __tablename__ = 'interview_sessions'
    __table_args__ = (UniqueConstraint('interview_url', name='uq_interview_url'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid4()), unique=True, index=True)
    vacancy_name: Mapped[str] = mapped_column(String(255), default='')
    interview_url: Mapped[str] = mapped_column(String(1000), default='')
    source_email: Mapped[str] = mapped_column(String(255), default='')
    subject: Mapped[str] = mapped_column(String(500), default='')
    state: Mapped[str] = mapped_column(String(50), default='new')
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    questions: Mapped[list['QuestionAnswer']] = relationship(back_populates='session', cascade='all, delete-orphan')


class QuestionAnswer(Base, TimestampMixin):
    __tablename__ = 'question_answers'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey('interview_sessions.id'))
    question: Mapped[str] = mapped_column(Text)
    draft_answer: Mapped[str] = mapped_column(Text, default='')
    final_answer: Mapped[str] = mapped_column(Text, default='')
    validation_status: Mapped[str] = mapped_column(String(50), default='draft')
    status: Mapped[str] = mapped_column(String(50), default='draft')
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    session: Mapped[InterviewSession] = relationship(back_populates='questions')


class AppLog(Base):
    __tablename__ = 'app_logs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(20), default='info')
    event_type: Mapped[str] = mapped_column(String(100), default='generic')
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppSetting(Base, TimestampMixin):
    __tablename__ = 'app_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    openrouter_api_key: Mapped[str] = mapped_column(Text, default='')
    openrouter_model: Mapped[str] = mapped_column(String(255), default='google/gemini-3.1-flash-lite-preview')
    pdf_engine: Mapped[str] = mapped_column(String(50), default='pdf-text')
    mail_provider: Mapped[str] = mapped_column(String(50), default='gmail_oauth')
    personal_email: Mapped[str] = mapped_column(String(255), default='')
    gmail_oauth_client_id: Mapped[str] = mapped_column(Text, default='')
    gmail_oauth_client_secret: Mapped[str] = mapped_column(Text, default='')
    gmail_oauth_redirect_uri: Mapped[str] = mapped_column(String(500), default='')
    gmail_oauth_refresh_token: Mapped[str] = mapped_column(Text, default='')
    gmail_oauth_access_token: Mapped[str] = mapped_column(Text, default='')
    gmail_oauth_state: Mapped[str] = mapped_column(String(255), default='')
    gmail_oauth_status: Mapped[str] = mapped_column(String(50), default='not_connected')
    imap_host: Mapped[str] = mapped_column(String(255), default='')
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_password: Mapped[str] = mapped_column(Text, default='')
    imap_folder: Mapped[str] = mapped_column(String(100), default='INBOX')
    mail_poll_enabled: Mapped[str] = mapped_column(String(10), default='no')
    mail_poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_email_uid: Mapped[str] = mapped_column(String(100), default='')
    telegram_api_id: Mapped[str] = mapped_column(String(50), default='')
    telegram_api_hash: Mapped[str] = mapped_column(Text, default='')
    telegram_phone_number: Mapped[str] = mapped_column(String(50), default='')
    telegram_session_string: Mapped[str] = mapped_column(Text, default='')
    telegram_2fa_password: Mapped[str] = mapped_column(Text, default='')
    telegram_phone_code_hash: Mapped[str] = mapped_column(Text, default='')
    telegram_auth_status: Mapped[str] = mapped_column(String(50), default='not_authorized')
    auto_send_telegram: Mapped[str] = mapped_column(String(10), default='no')
