from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

from app.schemas import CandidateProfilePayload


class ResumeService:
    def parse_pdf(self, resume_path: Path) -> CandidateProfilePayload:
        reader = PdfReader(str(resume_path))
        pages = [page.extract_text() or '' for page in reader.pages]
        raw_text = '\n'.join(pages).strip()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        full_name = lines[0] if lines else ''
        current_role = lines[1] if len(lines) > 1 else ''
        skills = self._collect_section(lines, ['skills', 'стек', 'навыки', 'tech stack'])
        projects = self._collect_section(lines, ['projects', 'проекты'])
        education = self._collect_section(lines, ['education', 'образование'])
        achievements = self._collect_section(lines, ['achievements', 'достижения'])
        experience_summary = self._summarize_experience(lines)
        english_level = self._find_value(lines, ['english', 'английский'])
        salary_expectation = self._find_value(lines, ['salary', 'зарплата'])
        work_format = self._find_value(lines, ['format', 'remote', 'офис', 'гибрид'])
        notice_period = self._find_value(lines, ['notice', 'выход', 'срок'])

        return CandidateProfilePayload(
            full_name=full_name,
            current_role=current_role,
            experience_summary=experience_summary,
            skills=skills,
            projects=projects,
            education=education,
            achievements=achievements,
            english_level=english_level,
            salary_expectation=salary_expectation,
            work_format=work_format,
            notice_period=notice_period,
            must_not_claim=[],
            source_resume_name=resume_path.name,
            raw_resume_text=raw_text,
        )

    def _collect_section(self, lines: list[str], markers: Iterable[str]) -> list[str]:
        collected: list[str] = []
        active = False
        for line in lines:
            normalized = line.lower().strip(':')
            if any(marker in normalized for marker in markers):
                active = True
                continue
            if active and self._looks_like_heading(normalized):
                break
            if active:
                collected.extend(self._split_list_line(line))
        return collected[:8]

    def _summarize_experience(self, lines: list[str]) -> str:
        for idx, line in enumerate(lines):
            lowered = line.lower()
            if 'summary' in lowered or 'о себе' in lowered or 'experience' in lowered:
                return ' '.join(lines[idx + 1: idx + 4])[:600]
        return ' '.join(lines[2:6])[:600]

    def _find_value(self, lines: list[str], markers: Iterable[str]) -> str:
        for line in lines:
            lowered = line.lower()
            if any(marker in lowered for marker in markers):
                parts = line.split(':', 1)
                if len(parts) == 2:
                    return parts[1].strip()[:100]
                return line[:100]
        return ''

    def _looks_like_heading(self, line: str) -> bool:
        headings = ['experience', 'skills', 'education', 'projects', 'achievements', 'summary', 'контакты', 'опыт', 'навыки', 'образование', 'проекты', 'достижения']
        return any(item == line.strip(':') for item in headings)

    def _split_list_line(self, line: str) -> list[str]:
        candidates = [part.strip(' -•\t') for part in line.replace(';', ',').split(',')]
        return [item for item in candidates if item]
