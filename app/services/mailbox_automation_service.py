import email
import imaplib
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

from sqlalchemy.orm import Session

from app.models import EmailLog
from app.schemas import EmailIngestPayload, LogEventPayload, SessionCreatePayload
from app.services.email_service import EmailService
from app.services.gmail_oauth_service import GmailAPIError, GmailOAuthService
from app.services.logging_service import LogService
from app.services.session_service import SessionService
from app.services.settings_service import SettingsService


class MailboxAutomationService:
    def __init__(self) -> None:
        self.email_service = EmailService()
        self.gmail_service = GmailOAuthService()
        self.session_service = SessionService()
        self.settings_service = SettingsService()

    def sync_latest_messages(self, db: Session) -> dict[str, Any]:
        settings = self.settings_service.get_or_create(db)
        if settings.mail_provider == 'gmail_oauth':
            return self._sync_gmail_messages(db, settings)
        return self._sync_imap_messages(db, settings)

    def _sync_imap_messages(self, db: Session, settings) -> dict[str, Any]:
        if not settings.personal_email or not settings.imap_host or not settings.imap_password:
            return {'status': 'skipped', 'reason': 'mailbox_not_configured'}

        processed = 0
        created_sessions = 0
        allowed = set(self.settings_service.sender_filters())

        with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as client:
            client.login(settings.personal_email, settings.imap_password)
            client.select(settings.imap_folder or 'INBOX')
            status, data = client.search(None, 'ALL')
            if status != 'OK':
                return {'status': 'error', 'reason': 'imap_search_failed'}
            uids = [item.decode('utf-8') for item in data[0].split() if item]
            if settings.last_email_uid:
                uids = [uid for uid in uids if uid > settings.last_email_uid]
            recent_uids = uids[-10:]
            for uid in recent_uids:
                status, msg_data = client.fetch(uid, '(RFC822)')
                if status != 'OK' or not msg_data or not msg_data[0]:
                    continue
                raw_message = msg_data[0][1]
                message = email.message_from_bytes(raw_message)
                sender = self.settings_service.normalized_sender(message.get('From', ''))
                if sender not in allowed:
                    settings.last_email_uid = uid
                    db.add(settings)
                    db.commit()
                    continue
                remote_message_id = (message.get('Message-ID') or '').strip()
                if remote_message_id and db.query(EmailLog).filter(EmailLog.remote_message_id == remote_message_id).first():
                    settings.last_email_uid = uid
                    db.add(settings)
                    db.commit()
                    continue

                subject = self._decode_header_value(message.get('Subject', ''))
                html_body, text_body = self._extract_bodies(message)
                received_at = parsedate_to_datetime(message.get('Date')) if message.get('Date') else None
                if received_at and received_at.tzinfo:
                    received_at = received_at.replace(tzinfo=None)

                created = self._ingest_parsed_message(
                    db,
                    uid,
                    remote_message_id,
                    EmailIngestPayload(
                        source_email=sender,
                        subject=subject,
                        html_body=html_body,
                        text_body=text_body,
                        received_at=received_at,
                    ),
                    created_via='imap_poll',
                )
                created_sessions += 1 if created else 0
                settings.last_email_uid = uid
                db.add(settings)
                db.commit()
                processed += 1

        LogService.write(
            db,
            LogEventPayload(
                event_type='mailbox_sync',
                message='IMAP mailbox sync completed',
                payload={'processed': processed, 'created_sessions': created_sessions},
            ),
        )
        return {'status': 'ok', 'provider': 'imap', 'processed': processed, 'created_sessions': created_sessions}

    def _sync_gmail_messages(self, db: Session, settings) -> dict[str, Any]:
        if not settings.personal_email or not settings.gmail_oauth_refresh_token:
            return {'status': 'skipped', 'reason': 'gmail_oauth_not_connected'}

        processed = 0
        created_sessions = 0
        try:
            messages = self._run_async(self.gmail_service.list_recent_invite_messages(settings))
        except GmailAPIError as exc:
            settings.gmail_oauth_status = f'error:{exc.reason or "gmail_api"}'
            db.add(settings)
            db.commit()
            raise
        settings.gmail_oauth_status = 'connected'
        db.add(settings)
        db.commit()
        for message in messages:
            parsed_message = self.gmail_service.parse_message(message)
            remote_uid = parsed_message['message_id']
            remote_message_id = parsed_message['remote_message_id'] or remote_uid
            if db.query(EmailLog).filter(EmailLog.remote_message_id == remote_message_id).first():
                continue
            created = self._ingest_parsed_message(
                db,
                remote_uid,
                remote_message_id,
                EmailIngestPayload(
                    source_email=parsed_message['source_email'],
                    subject=parsed_message['subject'],
                    html_body=parsed_message['html_body'],
                    text_body=parsed_message['text_body'],
                    received_at=parsed_message['received_at'],
                ),
                created_via='gmail_oauth',
            )
            created_sessions += 1 if created else 0
            settings.last_email_uid = remote_uid
            db.add(settings)
            db.commit()
            processed += 1

        LogService.write(
            db,
            LogEventPayload(
                event_type='mailbox_sync',
                message='Gmail OAuth mailbox sync completed',
                payload={'processed': processed, 'created_sessions': created_sessions},
            ),
        )
        return {'status': 'ok', 'provider': 'gmail_oauth', 'processed': processed, 'created_sessions': created_sessions}

    def _ingest_parsed_message(self, db: Session, remote_uid: str, remote_message_id: str, payload: EmailIngestPayload, created_via: str) -> bool:
        parsed = self.email_service.parse_invite(payload)
        log_entry = self.session_service.log_email(db, parsed, payload.html_body, payload.text_body)
        log_entry.remote_message_id = remote_message_id
        log_entry.remote_uid = remote_uid
        db.add(log_entry)
        db.commit()
        if parsed.matched:
            session = self.session_service.create_or_get_session(
                db,
                SessionCreatePayload(
                    vacancy_name=parsed.vacancy_name,
                    interview_url=parsed.interview_url,
                    source_email=parsed.source_email,
                    subject=parsed.subject,
                ),
            )
            session.meta = {**(session.meta or {}), 'created_via': created_via}
            db.add(session)
            db.commit()
            return True
        return False

    def _run_async(self, awaitable):
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        raise RuntimeError('Cannot run Gmail sync coroutine inside an active event loop') from None

    def _decode_header_value(self, value: str) -> str:
        try:
            return str(make_header(decode_header(value or '')))
        except Exception:
            return value or ''

    def _extract_bodies(self, message: Message) -> tuple[str, str]:
        html_body = ''
        text_body = ''
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                disposition = str(part.get('Content-Disposition', ''))
                if 'attachment' in disposition.lower():
                    continue
                payload = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                try:
                    decoded = payload.decode(charset, errors='ignore')
                except LookupError:
                    decoded = payload.decode('utf-8', errors='ignore')
                if content_type == 'text/html' and not html_body:
                    html_body = unescape(decoded)
                elif content_type == 'text/plain' and not text_body:
                    text_body = decoded
        else:
            payload = message.get_payload(decode=True) or b''
            charset = message.get_content_charset() or 'utf-8'
            decoded = payload.decode(charset, errors='ignore')
            if message.get_content_type() == 'text/html':
                html_body = unescape(decoded)
            else:
                text_body = decoded
        return html_body, text_body
