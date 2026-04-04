import re
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

from app.config import get_settings
from app.schemas import EmailIngestPayload, EmailParseResult

URL_PATTERN = re.compile(r'https?://[^\s"\'>]+', re.IGNORECASE)
HREF_PATTERN = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
INTERVIEW_HINTS = (
    'interview',
    'chat',
    'telegram',
    't.me',
    'hh.ru',
    'sberbank',
    'hrplatform',
    'pulse',
    'giga',
    'bot',
    'vacancy',
    'candidate',
)
IGNORED_HOST_MARKERS = (
    'w3.org',
    'schemas.microsoft.com',
    'fonts.googleapis.com',
    'fonts.gstatic.com',
    'google-analytics.com',
    'doubleclick.net',
    'facebook.com',
    'vk.com/share',
)


class EmailService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def parse_invite(self, payload: EmailIngestPayload) -> EmailParseResult:
        received_at = payload.received_at or datetime.utcnow()
        combined_body = '\n'.join(filter(None, [payload.html_body, payload.text_body]))
        normalized_sender = payload.source_email.lower()

        sender_match = any(item in normalized_sender for item in self.settings.target_email_senders)
        interview_url = self._extract_interview_url(combined_body)
        vacancy_name = self._extract_vacancy_name(payload.subject, combined_body)
        matched = sender_match and self.has_actionable_invite(interview_url)
        reason = ''
        if not matched:
            if not sender_match:
                reason = 'sender_not_allowed'
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
        candidates = [match.rstrip(').,]') for match in HREF_PATTERN.findall(text)]
        candidates.extend(raw_url.rstrip(').,]') for raw_url in URL_PATTERN.findall(text))
        scored_candidates: list[tuple[int, str]] = []
        for raw_url in candidates:
            url = raw_url.strip()
            if not url.startswith(('http://', 'https://')):
                continue
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            if not host or any(marker in host for marker in IGNORED_HOST_MARKERS):
                continue
            score = 0
            lowered = url.lower()
            if any(hint in lowered for hint in INTERVIEW_HINTS):
                score += 10
            if any(hint in parsed.path.lower() for hint in ['apply', 'invite', 'start', 'session']):
                score += 5
            if any(token in lowered for token in ['token=', 'interview', 'vacancy', 'candidate', 'chat', 'bot']):
                score += 4
            if host in {'t.me', 'telegram.me'} or host.endswith('.hh.ru') or host.endswith('sberbank.ru'):
                score += 7
            if any(lowered.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js']):
                score -= 10
            if score > 0:
                scored_candidates.append((score, url))
        if scored_candidates:
            scored_candidates.sort(key=lambda item: item[0], reverse=True)
            return scored_candidates[0][1]
        return ''

    def has_actionable_invite(self, interview_url: str) -> bool:
        if not interview_url:
            return False
        parsed = urlparse(interview_url)
        host = parsed.netloc.lower()
        return bool(host) and not any(marker in host for marker in IGNORED_HOST_MARKERS)

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
