from sqlalchemy.orm import Session

from app.models import EmailLog, InterviewSession, QuestionAnswer
from app.schemas import EmailParseResult, SessionCreatePayload


class SessionService:
    def create_or_get_session(self, db: Session, payload: SessionCreatePayload) -> InterviewSession:
        existing = db.query(InterviewSession).filter(InterviewSession.interview_url == payload.interview_url).first()
        if existing:
            return existing
        session = InterviewSession(
            vacancy_name=payload.vacancy_name,
            interview_url=payload.interview_url,
            source_email=payload.source_email,
            subject=payload.subject,
            state='new',
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    def log_email(self, db: Session, parsed: EmailParseResult, html_body: str, text_body: str) -> EmailLog:
        email_log = EmailLog(
            source_email=parsed.source_email,
            subject=parsed.subject,
            vacancy_name=parsed.vacancy_name,
            interview_url=parsed.interview_url,
            received_at=parsed.received_at,
            html_body=html_body,
            text_body=text_body,
            matched='yes' if parsed.matched else 'no',
            failure_reason=parsed.failure_reason,
        )
        db.add(email_log)
        db.commit()
        db.refresh(email_log)
        return email_log

    def add_question(self, db: Session, session: InterviewSession, question: str) -> QuestionAnswer:
        entry = QuestionAnswer(session_id=session.id, question=question, status='draft', validation_status='draft')
        session.state = 'active'
        db.add(entry)
        db.add(session)
        db.commit()
        db.refresh(entry)
        return entry
