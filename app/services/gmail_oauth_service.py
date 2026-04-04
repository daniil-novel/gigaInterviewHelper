import base64
import secrets
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import urlencode

import httpx

from app.models import AppSetting

GMAIL_READONLY_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
GMAIL_API_BASE = 'https://gmail.googleapis.com/gmail/v1/users/me'


class GmailAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, reason: str = '', activation_url: str = '', raw: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.activation_url = activation_url
        self.raw = raw or {}


class GmailOAuthService:
    def build_authorization_url(self, settings: AppSetting) -> tuple[str, str]:
        self._validate_client_settings(settings)
        state = secrets.token_urlsafe(24)
        params = {
            'client_id': settings.gmail_oauth_client_id,
            'redirect_uri': settings.gmail_oauth_redirect_uri,
            'response_type': 'code',
            'scope': GMAIL_READONLY_SCOPE,
            'access_type': 'offline',
            'include_granted_scopes': 'true',
            'prompt': 'consent',
            'state': state,
            'login_hint': settings.personal_email,
        }
        return f'{AUTH_URL}?{urlencode(params)}', state

    async def exchange_code(self, settings: AppSetting, code: str) -> dict[str, Any]:
        self._validate_client_settings(settings)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    'code': code,
                    'client_id': settings.gmail_oauth_client_id,
                    'client_secret': settings.gmail_oauth_client_secret,
                    'redirect_uri': settings.gmail_oauth_redirect_uri,
                    'grant_type': 'authorization_code',
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            )
            await self._raise_for_google_error(response, default_message='Google OAuth token exchange failed')
            return response.json()

    async def refresh_access_token(self, settings: AppSetting) -> str:
        if not settings.gmail_oauth_refresh_token:
            raise ValueError('Gmail refresh token is missing. Reconnect Gmail OAuth.')
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    'client_id': settings.gmail_oauth_client_id,
                    'client_secret': settings.gmail_oauth_client_secret,
                    'refresh_token': settings.gmail_oauth_refresh_token,
                    'grant_type': 'refresh_token',
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            )
            await self._raise_for_google_error(response, default_message='Failed to refresh Gmail OAuth access token')
            data = response.json()
        settings.gmail_oauth_access_token = data['access_token']
        settings.gmail_oauth_status = 'connected'
        return settings.gmail_oauth_access_token

    async def list_recent_invite_messages(self, settings: AppSetting) -> list[dict[str, Any]]:
        access_token = await self.refresh_access_token(settings)
        query = 'from:(noreply@hh.ru OR hrplatform@sberbank.ru) newer_than:30d'
        headers = {'Authorization': f'Bearer {access_token}'}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f'{GMAIL_API_BASE}/messages', headers=headers, params={'q': query, 'maxResults': 20})
            await self._raise_for_google_error(response, default_message='Failed to read Gmail messages')
            payload = response.json()
            messages = payload.get('messages', [])
            result: list[dict[str, Any]] = []
            for item in messages:
                detail = await client.get(f"{GMAIL_API_BASE}/messages/{item['id']}", headers=headers, params={'format': 'full'})
                await self._raise_for_google_error(detail, default_message='Failed to fetch Gmail message details')
                result.append(detail.json())
            return result

    async def diagnose(self, settings: AppSetting) -> dict[str, Any]:
        access_token = await self.refresh_access_token(settings)
        headers = {'Authorization': f'Bearer {access_token}'}
        result: dict[str, Any] = {'token_scope': '', 'gmail_status': 'unknown'}
        async with httpx.AsyncClient(timeout=30.0) as client:
            tokeninfo = await client.get('https://oauth2.googleapis.com/tokeninfo', params={'access_token': access_token})
            if tokeninfo.is_success:
                token_payload = tokeninfo.json()
                result['token_scope'] = token_payload.get('scope', '')
                result['token_audience'] = token_payload.get('aud', '')
            profile = await client.get(f'{GMAIL_API_BASE}/profile', headers=headers)
            if profile.is_success:
                result['gmail_status'] = 'ok'
                result['gmail_profile'] = profile.json()
                return result
            try:
                await self._raise_for_google_error(profile, default_message='Gmail profile check failed')
            except GmailAPIError as exc:
                result['gmail_status'] = 'error'
                result['gmail_error'] = {
                    'message': str(exc),
                    'reason': exc.reason,
                    'activation_url': exc.activation_url,
                    'status_code': exc.status_code,
                }
                return result
        return result

    def parse_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {entry.get('name', '').lower(): entry.get('value', '') for entry in payload.get('payload', {}).get('headers', [])}
        body = self._extract_body(payload.get('payload', {}))
        internal_date = payload.get('internalDate')
        received_at = None
        if internal_date:
            received_at = parsedate_to_datetime(headers.get('date')) if headers.get('date') else None
            if received_at and received_at.tzinfo:
                received_at = received_at.replace(tzinfo=None)
        return {
            'message_id': payload.get('id', ''),
            'remote_message_id': headers.get('message-id', ''),
            'source_email': self._normalize_from(headers.get('from', '')),
            'subject': headers.get('subject', ''),
            'html_body': body['html_body'],
            'text_body': body['text_body'],
            'received_at': received_at,
        }

    def _extract_body(self, part: dict[str, Any]) -> dict[str, str]:
        html_body = ''
        text_body = ''
        mime_type = part.get('mimeType', '')
        data = part.get('body', {}).get('data')
        if data:
            decoded = self._decode_base64url(data)
            if mime_type == 'text/html':
                html_body = unescape(decoded)
            elif mime_type == 'text/plain':
                text_body = decoded
        for subpart in part.get('parts', []) or []:
            nested = self._extract_body(subpart)
            html_body = html_body or nested['html_body']
            text_body = text_body or nested['text_body']
        return {'html_body': html_body, 'text_body': text_body}

    def _decode_base64url(self, value: str) -> str:
        padding = '=' * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode('utf-8')).decode('utf-8', errors='ignore')

    def _normalize_from(self, from_header: str) -> str:
        if '<' in from_header and '>' in from_header:
            return from_header.split('<', 1)[1].split('>', 1)[0].strip().lower()
        return from_header.strip().lower()

    def _validate_client_settings(self, settings: AppSetting) -> None:
        if not settings.gmail_oauth_client_id or not settings.gmail_oauth_client_secret or not settings.gmail_oauth_redirect_uri:
            raise ValueError('Gmail OAuth client_id, client_secret and redirect_uri must be configured')

    async def _raise_for_google_error(self, response: httpx.Response, *, default_message: str) -> None:
        if response.is_success:
            return
        payload: dict[str, Any] = {}
        try:
            payload = response.json()
        except Exception:
            response.raise_for_status()
        error = payload.get('error', {}) if isinstance(payload, dict) else {}
        message = error.get('message') or default_message
        reason = ''
        activation_url = ''
        for item in error.get('errors', []) or []:
            if item.get('reason'):
                reason = item['reason']
                break
        for detail in error.get('details', []) or []:
            metadata = detail.get('metadata', {})
            if metadata.get('activationUrl'):
                activation_url = metadata['activationUrl']
            if detail.get('reason') and not reason:
                reason = detail['reason']
        if reason in {'accessNotConfigured', 'SERVICE_DISABLED'}:
            message = f'Gmail API is disabled in Google Cloud project. Enable it and retry. {activation_url or ""}'.strip()
        elif reason == 'insufficientPermissions':
            message = 'Gmail OAuth token does not have enough permissions. Reconnect Gmail OAuth and grant Gmail access again.'
        raise GmailAPIError(message, status_code=response.status_code, reason=reason, activation_url=activation_url, raw=payload)
