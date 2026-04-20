from email.utils import parseaddr
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting
from app.schemas import MailboxSettingsPayload, OpenRouterSettingsPayload, TelegramSettingsPayload


class SettingsService:
    def __init__(self) -> None:
        self.defaults = get_settings()

    def get_or_create(self, db: Session) -> AppSetting:
        entry = db.query(AppSetting).order_by(AppSetting.id.asc()).first()
        if entry:
            changed = False
            if not entry.openrouter_model:
                entry.openrouter_model = self.defaults.openrouter_model
                changed = True
            if entry.imap_port is None:
                entry.imap_port = 993
                changed = True
            if entry.mail_poll_interval_seconds is None:
                entry.mail_poll_interval_seconds = 60
                changed = True
            if not entry.telegram_auto_reply_paused:
                entry.telegram_auto_reply_paused = 'no'
                changed = True
            if changed:
                db.add(entry)
                db.commit()
                db.refresh(entry)
            return entry
        entry = AppSetting(
            openrouter_api_key=self.defaults.openrouter_api_key,
            openrouter_model=self.defaults.openrouter_model,
            pdf_engine='pdf-text',
            mail_provider='gmail_oauth',
            imap_port=993,
            imap_folder='INBOX',
            mail_poll_enabled='no',
            mail_poll_interval_seconds=60,
            telegram_auth_status='not_authorized',
            auto_send_telegram='no',
            telegram_auto_reply_paused='no',
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    def to_payload(self, entry: AppSetting) -> OpenRouterSettingsPayload:
        return OpenRouterSettingsPayload(
            openrouter_api_key=entry.openrouter_api_key,
            openrouter_model=entry.openrouter_model,
            pdf_engine=entry.pdf_engine,
        )

    def update_openrouter(self, db: Session, payload: OpenRouterSettingsPayload) -> AppSetting:
        entry = self.get_or_create(db)
        entry.openrouter_api_key = payload.openrouter_api_key.strip()
        entry.openrouter_model = payload.openrouter_model.strip() or self.defaults.openrouter_model
        entry.pdf_engine = payload.pdf_engine.strip() or 'pdf-text'
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    def update_mailbox(self, db: Session, payload: MailboxSettingsPayload) -> AppSetting:
        entry = self.get_or_create(db)
        entry.mail_provider = payload.mail_provider.strip() or 'gmail_oauth'
        entry.personal_email = payload.personal_email.strip()
        entry.gmail_oauth_client_id = payload.gmail_oauth_client_id.strip()
        entry.gmail_oauth_client_secret = payload.gmail_oauth_client_secret.strip()
        entry.gmail_oauth_redirect_uri = payload.gmail_oauth_redirect_uri.strip()
        entry.imap_host = payload.imap_host.strip()
        entry.imap_port = payload.imap_port
        entry.imap_password = payload.imap_password.strip()
        entry.imap_folder = payload.imap_folder.strip() or 'INBOX'
        entry.mail_poll_enabled = 'yes' if payload.mail_poll_enabled else 'no'
        entry.mail_poll_interval_seconds = max(15, payload.mail_poll_interval_seconds)
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    def update_telegram(self, db: Session, payload: TelegramSettingsPayload) -> AppSetting:
        entry = self.get_or_create(db)
        entry.telegram_api_id = payload.telegram_api_id.strip()
        entry.telegram_api_hash = payload.telegram_api_hash.strip()
        entry.telegram_phone_number = payload.telegram_phone_number.strip()
        entry.telegram_2fa_password = payload.telegram_2fa_password.strip()
        entry.auto_send_telegram = 'yes' if payload.auto_send_telegram else 'no'
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    def save_telegram_runtime(self, db: Session, entry: AppSetting) -> AppSetting:
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    def save_runtime(self, db: Session, entry: AppSetting) -> AppSetting:
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    def sender_filters(self) -> list[str]:
        return ['noreply@hh.ru', 'hrplatform@sberbank.ru']

    def masked_key(self, key: str) -> str:
        if not key:
            return 'не задан'
        if len(key) <= 10:
            return '***'
        return f'{key[:8]}...{key[-4:]}'

    def masked_secret(self, value: str) -> str:
        if not value:
            return 'не задан'
        if len(value) <= 8:
            return '***'
        return f'{value[:4]}...{value[-2:]}'

    def normalized_sender(self, value: str) -> str:
        _, address = parseaddr(value or '')
        return address.lower().strip()
