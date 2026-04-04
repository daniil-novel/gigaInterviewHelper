from urllib.parse import parse_qs, urlparse

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.custom import Message
except ImportError:  # pragma: no cover - optional dependency at runtime
    TelegramClient = None
    StringSession = None
    Message = None

from app.models import AppSetting, InterviewSession


class TelegramAutomationService:
    async def request_login_code(self, settings: AppSetting) -> str:
        self._ensure_available()
        if not settings.telegram_api_id or not settings.telegram_api_hash or not settings.telegram_phone_number:
            raise ValueError('Telegram credentials are incomplete')
        client = TelegramClient(StringSession(settings.telegram_session_string or ''), int(settings.telegram_api_id), settings.telegram_api_hash)
        await client.connect()
        try:
            result = await client.send_code_request(settings.telegram_phone_number)
            settings.telegram_phone_code_hash = result.phone_code_hash
            settings.telegram_auth_status = 'code_sent'
            if await client.is_user_authorized():
                settings.telegram_auth_status = 'authorized'
                settings.telegram_session_string = client.session.save()
            return settings.telegram_phone_code_hash
        finally:
            await client.disconnect()

    async def verify_login_code(self, settings: AppSetting, code: str, password: str = '') -> None:
        self._ensure_available()
        if not settings.telegram_phone_code_hash:
            raise ValueError('Telegram code was not requested yet')
        client = TelegramClient(StringSession(settings.telegram_session_string or ''), int(settings.telegram_api_id), settings.telegram_api_hash)
        await client.connect()
        try:
            await client.sign_in(
                phone=settings.telegram_phone_number,
                code=code,
                phone_code_hash=settings.telegram_phone_code_hash,
            )
        except Exception as exc:
            if password:
                await client.sign_in(password=password)
            else:
                raise exc
        settings.telegram_session_string = client.session.save()
        settings.telegram_auth_status = 'authorized'
        settings.telegram_phone_code_hash = ''
        await client.disconnect()

    async def send_interview_answer(self, settings: AppSetting, session: InterviewSession, answer_text: str) -> str:
        self._ensure_available()
        if not settings.telegram_session_string:
            raise ValueError('Telegram is not authorized yet')
        if not session.interview_url:
            raise ValueError('Interview URL is empty')

        username, start_payload = self._parse_telegram_target(session.interview_url)
        if not username:
            raise ValueError('Interview URL is not a Telegram link')

        client = TelegramClient(StringSession(settings.telegram_session_string), int(settings.telegram_api_id), settings.telegram_api_hash)
        await client.connect()
        try:
            entity = await client.get_entity(username)
            session_meta = session.meta or {}
            if start_payload and not session_meta.get('telegram_bootstrap_sent'):
                await client.send_message(entity, f'/start {start_payload}')
                session_meta['telegram_bootstrap_sent'] = True
                session.meta = session_meta
            message: Message = await client.send_message(entity, answer_text)
            return str(message.id)
        finally:
            await client.disconnect()

    def _parse_telegram_target(self, interview_url: str) -> tuple[str, str]:
        parsed = urlparse(interview_url)
        if parsed.netloc.lower() not in {'t.me', 'telegram.me', 'www.t.me'}:
            return '', ''
        username = parsed.path.strip('/').split('/')[0]
        query = parse_qs(parsed.query)
        start_payload = ''
        for key in ['start', 'startapp', 'startattach']:
            if query.get(key):
                start_payload = query[key][0]
                break
        return username, start_payload

    def _ensure_available(self) -> None:
        if TelegramClient is None or StringSession is None:
            raise RuntimeError("Telegram support requires 'telethon'. Install dependencies with: .\\.venv\\Scripts\\python -m pip install -r requirements.txt")
