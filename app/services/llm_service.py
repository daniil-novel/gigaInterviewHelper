import base64
import json
from pathlib import Path
from textwrap import shorten

import httpx

from app.config import get_settings
from app.schemas import CandidateProfilePayload, OpenRouterSettingsPayload


SYSTEM_PROMPT = """Ты помощник кандидата на AI-интервью. Отвечай только на основе профиля кандидата. Пиши от первого лица, коротко, естественно, в формате 1-4 предложений. Если в профиле не хватает фактов, честно скажи, что ответ стоит уточнить вручную."""

RESUME_PARSE_PROMPT = """Ты карьерный ассистент и очень аккуратный resume parser. Тебе дадут PDF-резюме кандидата. Проанализируй только содержимое документа и верни JSON-объект без лишнего текста.

Правила:
1. Не выдумывай факты.
2. Если поля нет в резюме, оставь пустую строку или пустой массив.
3. Поле must_not_claim заполни фактами, которые не подтверждены прямо в резюме, но которые обычно легко приписать по ошибке. Если таких явных рисков нет, верни пустой массив.
4. experience_summary сделай кратким и фактическим, максимум 500 символов.
5. parsing_notes используй для коротких замечаний: какие поля пришлось оставить пустыми или что выглядит неоднозначно.
6. Ответ верни строго в JSON.

Схема JSON:
{
  "full_name": "string",
  "current_role": "string",
  "experience_summary": "string",
  "skills": ["string"],
  "projects": ["string"],
  "education": ["string"],
  "achievements": ["string"],
  "english_level": "string",
  "salary_expectation": "string",
  "work_format": "string",
  "notice_period": "string",
  "must_not_claim": ["string"],
  "parsing_notes": "string"
}"""


class LLMService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def generate_answer(
        self,
        question: str,
        profile: CandidateProfilePayload,
        openrouter: OpenRouterSettingsPayload,
    ) -> tuple[str, str]:
        if openrouter.openrouter_api_key:
            try:
                return await self._call_openrouter_answer(question, profile, openrouter), 'openrouter'
            except Exception:
                pass
        return self._fallback_answer(question, profile), 'fallback'

    async def parse_resume_pdf(
        self,
        resume_path: Path,
        openrouter: OpenRouterSettingsPayload,
    ) -> CandidateProfilePayload:
        if not openrouter.openrouter_api_key:
            raise ValueError('OpenRouter API key is not set')
        parsed = await self._call_openrouter_resume_parse(resume_path, openrouter)
        return CandidateProfilePayload(
            full_name=parsed.get('full_name', ''),
            current_role=parsed.get('current_role', ''),
            experience_summary=parsed.get('experience_summary', ''),
            skills=self._ensure_list(parsed.get('skills')),
            projects=self._ensure_list(parsed.get('projects')),
            education=self._ensure_list(parsed.get('education')),
            achievements=self._ensure_list(parsed.get('achievements')),
            english_level=parsed.get('english_level', ''),
            salary_expectation=parsed.get('salary_expectation', ''),
            work_format=parsed.get('work_format', ''),
            notice_period=parsed.get('notice_period', ''),
            must_not_claim=self._ensure_list(parsed.get('must_not_claim')),
            source_resume_name=resume_path.name,
            raw_resume_text=json.dumps(parsed, ensure_ascii=False, indent=2),
            parsing_notes=parsed.get('parsing_notes', ''),
            last_parsed_with_model=openrouter.openrouter_model,
        )

    async def _call_openrouter_answer(
        self,
        question: str,
        profile: CandidateProfilePayload,
        openrouter: OpenRouterSettingsPayload,
    ) -> str:
        headers = self._headers(openrouter)
        body = {
            'model': openrouter.openrouter_model,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': json.dumps(
                        {
                            'question': question,
                            'candidate_profile': profile.model_dump(),
                            'output_schema': {'answer': 'string'},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=35.0) as client:
            response = await client.post('https://openrouter.ai/api/v1/chat/completions', headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        content = payload['choices'][0]['message']['content']
        parsed = json.loads(content)
        return parsed['answer'].strip()

    async def _call_openrouter_resume_parse(
        self,
        resume_path: Path,
        openrouter: OpenRouterSettingsPayload,
    ) -> dict:
        encoded = base64.b64encode(resume_path.read_bytes()).decode('ascii')
        headers = self._headers(openrouter)
        body = {
            'model': openrouter.openrouter_model,
            'plugins': [{'id': 'file-parser', 'pdf': {'engine': openrouter.pdf_engine}}],
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': RESUME_PARSE_PROMPT},
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': 'Проанализируй это резюме и заполни профиль кандидата по схеме.'},
                        {
                            'type': 'file',
                            'file': {
                                'filename': resume_path.name,
                                'file_data': f'data:application/pdf;base64,{encoded}',
                            },
                        },
                    ],
                },
            ],
        }
        async with httpx.AsyncClient(timeout=80.0) as client:
            response = await client.post('https://openrouter.ai/api/v1/chat/completions', headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        content = payload['choices'][0]['message']['content']
        if isinstance(content, list):
            content = ''.join(part.get('text', '') for part in content if isinstance(part, dict))
        return json.loads(content)

    def _headers(self, openrouter: OpenRouterSettingsPayload) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {openrouter.openrouter_api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': self.settings.app_base_url,
            'X-Title': self.settings.app_name,
        }

    def _fallback_answer(self, question: str, profile: CandidateProfilePayload) -> str:
        lead = self._pick_lead(question, profile)
        facts = []
        if profile.current_role:
            facts.append(f'Сейчас я работаю как {profile.current_role}.')
        if profile.experience_summary:
            facts.append(shorten(profile.experience_summary, width=180, placeholder='...'))
        if profile.skills:
            facts.append(f"Мой основной стек: {', '.join(profile.skills[:5])}.")
        if profile.projects:
            facts.append(f"Из релевантного могу опереться на проекты: {', '.join(profile.projects[:2])}.")
        if not facts:
            return 'Могу ответить честно только после того, как профиль будет заполнен точнее. Здесь лучше быстро уточнить детали вручную.'
        answer = ' '.join([lead] + facts[:3])
        return shorten(answer, width=self.settings.max_answer_chars, placeholder='...')

    def _pick_lead(self, question: str, profile: CandidateProfilePayload) -> str:
        lowered = question.lower()
        if 'why' in lowered or 'почему' in lowered:
            return 'Мне интересна роль, где можно опираться на мой текущий опыт и быстро приносить пользу.'
        if 'salary' in lowered or 'зарплат' in lowered:
            return f'По ожиданиям ориентируюсь на {profile.salary_expectation}.' if profile.salary_expectation else 'По ожиданиям готов обсуждать вилку после сверки задач и зоны ответственности.'
        if 'english' in lowered or 'англий' in lowered:
            return f'По английскому у меня уровень {profile.english_level}.' if profile.english_level else 'Английский лучше уточнить вручную, чтобы не завысить уровень.'
        return 'Коротко по делу:'

    def _ensure_list(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []
