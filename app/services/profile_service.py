from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models import CandidateProfile
from app.schemas import CandidateProfilePayload


class ProfileService:
    def __init__(self) -> None:
        pass

    def get_or_create_profile(self, db: Session) -> CandidateProfile:
        profile = db.query(CandidateProfile).order_by(CandidateProfile.id.asc()).first()
        if profile:
            return profile
        profile = CandidateProfile()
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile

    def to_payload(self, profile: CandidateProfile) -> CandidateProfilePayload:
        return CandidateProfilePayload(
            full_name=profile.full_name,
            current_role=profile.current_role,
            experience_summary=profile.experience_summary,
            skills=profile.skills or [],
            projects=profile.projects or [],
            education=profile.education or [],
            achievements=profile.achievements or [],
            english_level=profile.english_level,
            salary_expectation=profile.salary_expectation,
            work_format=profile.work_format,
            notice_period=profile.notice_period,
            must_not_claim=profile.must_not_claim or [],
            source_resume_name=profile.source_resume_name,
            raw_resume_text=profile.raw_resume_text,
            parsing_notes=profile.parsing_notes,
            last_parsed_with_model=profile.last_parsed_with_model,
        )

    def update_profile(self, db: Session, payload: CandidateProfilePayload) -> CandidateProfile:
        profile = self.get_or_create_profile(db)
        for key, value in payload.model_dump().items():
            if key in {'source_resume_name', 'raw_resume_text', 'parsing_notes', 'last_parsed_with_model'} and value in ('', []):
                continue
            setattr(profile, key, value)
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile

    async def store_resume_file(self, file: UploadFile) -> Path:
        uploads_dir = Path('uploads')
        uploads_dir.mkdir(exist_ok=True)
        target = uploads_dir / (file.filename or 'resume.pdf')
        content = await file.read()
        target.write_bytes(content)
        return target
