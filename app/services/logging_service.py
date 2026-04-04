from sqlalchemy.orm import Session

from app.models import AppLog
from app.schemas import LogEventPayload


class LogService:
    @staticmethod
    def write(db: Session, data: LogEventPayload) -> None:
        entry = AppLog(
            level=data.level,
            event_type=data.event_type,
            message=data.message,
            payload=data.payload,
        )
        db.add(entry)
        db.commit()
