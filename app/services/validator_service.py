from typing import Iterable

from app.config import get_settings
from app.schemas import CandidateProfilePayload


class ValidationResult:
    def __init__(self, status: str, warnings: list[str], needs_review: bool) -> None:
        self.status = status
        self.warnings = warnings
        self.needs_review = needs_review


class AnswerValidator:
    def __init__(self) -> None:
        self.settings = get_settings()

    def validate(self, answer: str, profile: CandidateProfilePayload) -> ValidationResult:
        warnings: list[str] = []
        trimmed = answer.strip()
        if not trimmed:
            warnings.append('Пустой ответ.')
        if len(trimmed) > self.settings.max_answer_chars:
            warnings.append('Ответ получился слишком длинным.')
        if self._sentence_count(trimmed) > self.settings.max_answer_sentences:
            warnings.append('Слишком много предложений для Telegram-стиля.')
        if any(item.lower() in trimmed.lower() for item in profile.must_not_claim if item):
            warnings.append('Ответ затрагивает стоп-факты из профиля.')
        if any(token in trimmed.lower() for token in ['лучший в мире', 'гарантирую', 'безупречно']):
            warnings.append('Похоже на неподтвержденное обещание.')

        status = 'approved' if not warnings else 'needs_review'
        return ValidationResult(status=status, warnings=warnings, needs_review=bool(warnings))

    def _sentence_count(self, text: str) -> int:
        return len([chunk for chunk in text.replace('!', '.').replace('?', '.').split('.') if chunk.strip()])

    def profile_tokens(self, profile: CandidateProfilePayload) -> set[str]:
        tokens: set[str] = set()
        for item in [profile.full_name, profile.current_role, profile.experience_summary, profile.english_level, profile.salary_expectation, profile.work_format, profile.notice_period]:
            tokens.update(self._tokenize(item))
        for group in [profile.skills, profile.projects, profile.education, profile.achievements]:
            for item in group:
                tokens.update(self._tokenize(item))
        return tokens

    def _tokenize(self, text: str) -> Iterable[str]:
        for word in text.lower().replace('/', ' ').replace(',', ' ').split():
            cleaned = word.strip('.:;()[]{}')
            if len(cleaned) > 2:
                yield cleaned
