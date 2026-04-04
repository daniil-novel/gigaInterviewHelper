from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()
connect_args = {'check_same_thread': False} if settings.database_url.startswith('sqlite') else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_sqlite_migrations() -> None:
    if not settings.database_url.startswith('sqlite'):
        return

    with engine.begin() as connection:
        inspector = inspect(connection)
        if inspector.has_table('candidate_profiles'):
            columns = {item['name'] for item in inspector.get_columns('candidate_profiles')}
            if 'parsing_notes' not in columns:
                connection.execute(text("ALTER TABLE candidate_profiles ADD COLUMN parsing_notes TEXT DEFAULT ''"))
            if 'last_parsed_with_model' not in columns:
                connection.execute(text("ALTER TABLE candidate_profiles ADD COLUMN last_parsed_with_model VARCHAR(255) DEFAULT ''"))
        if inspector.has_table('email_logs'):
            columns = {item['name'] for item in inspector.get_columns('email_logs')}
            if 'remote_message_id' not in columns:
                connection.execute(text("ALTER TABLE email_logs ADD COLUMN remote_message_id VARCHAR(255) DEFAULT ''"))
            if 'remote_uid' not in columns:
                connection.execute(text("ALTER TABLE email_logs ADD COLUMN remote_uid VARCHAR(100) DEFAULT ''"))
        if inspector.has_table('interview_sessions'):
            columns = {item['name'] for item in inspector.get_columns('interview_sessions')}
            if 'meta' not in columns:
                connection.execute(text("ALTER TABLE interview_sessions ADD COLUMN meta JSON DEFAULT '{}'"))
        if inspector.has_table('app_settings'):
            columns = {item['name'] for item in inspector.get_columns('app_settings')}
            additions = {
                'personal_email': "ALTER TABLE app_settings ADD COLUMN personal_email VARCHAR(255) DEFAULT ''",
                'mail_provider': "ALTER TABLE app_settings ADD COLUMN mail_provider VARCHAR(50) DEFAULT 'gmail_oauth'",
                'gmail_oauth_client_id': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_client_id TEXT DEFAULT ''",
                'gmail_oauth_client_secret': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_client_secret TEXT DEFAULT ''",
                'gmail_oauth_redirect_uri': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_redirect_uri VARCHAR(500) DEFAULT ''",
                'gmail_oauth_refresh_token': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_refresh_token TEXT DEFAULT ''",
                'gmail_oauth_access_token': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_access_token TEXT DEFAULT ''",
                'gmail_oauth_state': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_state VARCHAR(255) DEFAULT ''",
                'gmail_oauth_status': "ALTER TABLE app_settings ADD COLUMN gmail_oauth_status VARCHAR(50) DEFAULT 'not_connected'",
                'imap_host': "ALTER TABLE app_settings ADD COLUMN imap_host VARCHAR(255) DEFAULT ''",
                'imap_port': "ALTER TABLE app_settings ADD COLUMN imap_port INTEGER DEFAULT 993",
                'imap_password': "ALTER TABLE app_settings ADD COLUMN imap_password TEXT DEFAULT ''",
                'imap_folder': "ALTER TABLE app_settings ADD COLUMN imap_folder VARCHAR(100) DEFAULT 'INBOX'",
                'mail_poll_enabled': "ALTER TABLE app_settings ADD COLUMN mail_poll_enabled VARCHAR(10) DEFAULT 'no'",
                'mail_poll_interval_seconds': "ALTER TABLE app_settings ADD COLUMN mail_poll_interval_seconds INTEGER DEFAULT 60",
                'last_email_uid': "ALTER TABLE app_settings ADD COLUMN last_email_uid VARCHAR(100) DEFAULT ''",
                'telegram_api_id': "ALTER TABLE app_settings ADD COLUMN telegram_api_id VARCHAR(50) DEFAULT ''",
                'telegram_api_hash': "ALTER TABLE app_settings ADD COLUMN telegram_api_hash TEXT DEFAULT ''",
                'telegram_phone_number': "ALTER TABLE app_settings ADD COLUMN telegram_phone_number VARCHAR(50) DEFAULT ''",
                'telegram_session_string': "ALTER TABLE app_settings ADD COLUMN telegram_session_string TEXT DEFAULT ''",
                'telegram_2fa_password': "ALTER TABLE app_settings ADD COLUMN telegram_2fa_password TEXT DEFAULT ''",
                'telegram_phone_code_hash': "ALTER TABLE app_settings ADD COLUMN telegram_phone_code_hash TEXT DEFAULT ''",
                'telegram_auth_status': "ALTER TABLE app_settings ADD COLUMN telegram_auth_status VARCHAR(50) DEFAULT 'not_authorized'",
                'auto_send_telegram': "ALTER TABLE app_settings ADD COLUMN auto_send_telegram VARCHAR(10) DEFAULT 'no'",
            }
            for name, sql in additions.items():
                if name not in columns:
                    connection.execute(text(sql))
