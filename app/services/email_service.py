import re
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

from app.config import get_settings
from app.schemas import EmailIngestPayload, EmailParseResult

URL_PATTERN = re.compile(r'https?://[^\s"\'>]+', re.IGNORECASE)


class EmailService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def parse_invite(self, payload: EmailIngestPayload) -> EmailParseResult:
        received_at = payload.received_at or datetime.utcnow()
        combined_body = '\n'.join(filter(None, [payload.html_body, payload.text_body]))
        normalized_subject = payload.subject.lower()
        normalized_sender = payload.source_email.lower()

        sender_match = any(item in normalized_sender for item in self.settings.target_email_senders)
        subject_match = any(keyword in normalized_subject for keyword in self.settings.target_email_subject_keywords)
        interview_url = self._extract_interview_url(combined_body)
        vacancy_name = self._extract_vacancy_name(payload.subject, combined_body)
        matched = sender_match and subject_match and bool(interview_url)
        reason = ''
        if not matched:
            if not sender_match:
                reason = 'sender_not_allowed'
            elif not subject_match:
                reason = 'subject_not_matched'
            else:
                reason = 'interview_url_not_found'

        return EmailParseResult(
            matched=matched,
            source_email=payload.source_email,
            subject=payload.subject,
            vacancy_name=vacancy_name,
            interview_url=interview_url,
            received_at=received_at,
            failure_reason=reason,
        )

    def _extract_interview_url(self, body: str) -> str:
        text = unescape(body)
        for raw_url in URL_PATTERN.findall(text):
            url = raw_url.rstrip(').,]')
            host = urlparse(url).netloc.lower()
            if any(marker in host for marker in ['telegram', 't.me', 'interview', 'chat']) or 'http' in url:
                return url
        return ''

    def _extract_vacancy_name(self, subject: str, body: str) -> str:
        patterns = [
            re.compile(r'ваканси[яи]\s*[:\-]\s*(.+)', re.IGNORECASE),
            re.compile(r'position\s*[:\-]\s*(.+)', re.IGNORECASE),
            re.compile(r'рол[ья]\s*[:\-]\s*(.+)', re.IGNORECASE),
        ]
        for source in [subject, body]:
            for pattern in patterns:
                match = pattern.search(source)
                if match:
                    return match.group(1).strip()[:255]

        separators = [' на вакансию ', ' for ', ' позиция ', ' vacancy ']
        lowered = subject.lower()
        for separator in separators:
            if separator in lowered:
                return subject[lowered.index(separator) + len(separator):].strip()[:255]
        return subject[:255]
