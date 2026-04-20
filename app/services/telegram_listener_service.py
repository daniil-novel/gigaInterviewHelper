"""Background Telegram listener that auto-replies to bot questions using the LLM.

Design:
- Runs as an asyncio task alongside FastAPI.
- Connects as the user's Telethon client using the persisted session string.
- Waits 10 seconds after each bot message so multi-part prompts can finish before
  generating a single reply.
- Polls Telegram every 30 seconds for missed unanswered bot messages so the app
  can recover after restarts or temporary listener failures.
- Detects recruiter closing and feedback/rating messages and never replies to
  those messages.
- Tries to sync the active InterviewSession by matching the vacancy mentioned in
  the bot message against known sessions for the same Telegram bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
except ImportError:  # pragma: no cover
    TelegramClient = None
    StringSession = None
    events = None

from app.database import SessionLocal
from app.models import InterviewSession, QuestionAnswer
from app.schemas import LogEventPayload
from app.services.llm_service import LLMService
from app.services.logging_service import LogService
from app.services.profile_service import ProfileService
from app.services.settings_service import SettingsService
from app.services.validator_service import AnswerValidator


logger = logging.getLogger(__name__)

BOT_REPLY_DEBOUNCE_SECONDS = 10
UNANSWERED_SCAN_INTERVAL_SECONDS = 30
SESSION_MATCH_THRESHOLD = 18
FINAL_MESSAGE_MARKERS = (
    'диалог по вакансии заверш',
    'диалог заверш',
    'ваш отклик уже у рекрутера',
    'отклик уже у рекрутера',
    'рекрутер уже рассматривает ваш отклик',
    'мы уже передали ваш отклик рекрутеру',
)
FEEDBACK_MESSAGE_MARKERS = (
    'оцените диалог',
    'оцените общение',
    'оцените giga',
    'оцените gigarecruiter',
    'оцените гига',
    'оцените работу',
    'оцените чат',
    'поставьте оценку',
    'ваша оценка',
    'поделитесь впечатлением',
    'насколько вам понравилось',
)
STATUS_MESSAGE_MARKERS = (
    'ai рекрутер',
    'ai-рекрутер',
    'ваш ai рекрутер',
    'ваш ai-рекрутер',
    'beira',
    'бира',
    'мы рассматриваем вашу кандидатуру',
    'рассматриваем вашу кандидатуру',
    'по вакансии',
    'на позицию',
    'на вакансию',
    'вернемся с обратной связью',
    'вернемся к вам с обратной связью',
    'буду задавать вопросы',
    'готовы начать',
    'для продолжения отправьте /start',
    'для старта отправьте /start',
)
PROMPT_MARKERS = (
    'расскажите',
    'поделитесь',
    'опишите',
    'почему',
    'зачем',
    'что для вас',
    'что вам',
    'что вы',
    'как вы',
    'какой у вас',
    'какие у вас',
    'какой формат работы',
    'готовы рассматривать',
    'чем вы',
    'уточните',
    'приведите пример',
    'напишите',
    'ответьте',
)


@dataclass
class PendingChatBatch:
    username: str
    events: list[Any] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


class TelegramListenerService:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[TelegramClient] = None
        self._stop = asyncio.Event()
        self._llm = LLMService()
        self._profile = ProfileService()
        self._settings = SettingsService()
        self._validator = AnswerValidator()
        self._pending_batches: dict[int, PendingChatBatch] = {}
        self._pending_lock = asyncio.Lock()
        self._processing_chat_keys: set[int] = set()
        # Telethon message ids are scoped per chat, so dedupe uses (chat_id, message_id).
        self._seen_message_keys: set[tuple[int, int]] = set()

    async def start(self) -> None:
        if TelegramClient is None:
            logger.warning('telethon not installed; telegram listener disabled')
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name='telegram-listener')

    async def stop(self) -> None:
        self._stop.set()
        await self._cancel_pending_batches()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _cancel_pending_batches(self) -> None:
        async with self._pending_lock:
            tasks = [batch.task for batch in self._pending_batches.values() if batch.task]
            self._pending_batches.clear()
            self._processing_chat_keys.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # --------------------------------------------------------------------- run

    async def _run_forever(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                handled = await self._run_once()
                backoff = 5 if handled else backoff
                if not handled:
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('telegram listener crashed, retrying in %ss', backoff)
                self._log_error('telegram_listener_crash', str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _run_once(self) -> bool:
        """Returns True if we connected and the client ran (and then disconnected)."""
        db = SessionLocal()
        try:
            entry = self._settings.get_or_create(db)
            api_id_raw = (entry.telegram_api_id or '').strip()
            api_hash = (entry.telegram_api_hash or '').strip()
            session_string = (entry.telegram_session_string or '').strip()
            status = entry.telegram_auth_status
            auto_send = entry.auto_send_telegram == 'yes'
        finally:
            db.close()

        if not api_id_raw or not api_hash or not session_string or status != 'authorized':
            return False
        if not auto_send:
            return False
        api_id = int(api_id_raw)

        self._client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await self._client.connect()
        poll_task: Optional[asyncio.Task] = None
        try:
            if not await self._client.is_user_authorized():
                logger.info('telegram listener: session not authorized, waiting')
                return False

            self._client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))

            # Recover missed messages before sending any new bootstrap command.
            await self._check_unanswered_messages()
            await self._bootstrap_pending_sessions()
            poll_task = asyncio.create_task(
                self._poll_unanswered_loop(),
                name='telegram-unanswered-poll',
            )

            self._log_info('telegram_listener_started', 'Telegram auto-listener is active')
            logger.info('telegram listener connected and listening')

            await self._client.run_until_disconnected()
            return True
        finally:
            if poll_task:
                poll_task.cancel()
                try:
                    await poll_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _poll_unanswered_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(UNANSWERED_SCAN_INTERVAL_SECONDS)
                if self._stop.is_set():
                    break
                await self._check_unanswered_messages()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('unanswered telegram scan failed')
                self._log_error('telegram_unanswered_scan_failed', str(exc))

    async def _check_unanswered_messages(self) -> None:
        if self._client is None:
            return
        for username in self._poll_candidates():
            try:
                entity = await self._client.get_entity(username)
                chat_key = int(getattr(entity, 'id', 0) or 0)
                if await self._chat_is_busy(chat_key):
                    continue
                inbound_cluster = await self._recent_inbound_cluster(entity)
                if not inbound_cluster:
                    continue
                batch = PendingChatBatch(
                    username=username,
                    texts=[text for _, text in inbound_cluster],
                )
                await self._process_batch_guarded(chat_key, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('failed to inspect unanswered messages for @%s', username)
                self._log_error('telegram_unanswered_chat_failed', f'{username}: {exc}')

    def _poll_candidates(self) -> list[str]:
        db = SessionLocal()
        try:
            sessions = (
                db.query(InterviewSession)
                .filter(InterviewSession.state.in_(['new', 'active', 'waiting_approval']))
                .order_by(InterviewSession.updated_at.desc())
                .all()
            )
            result: list[str] = []
            seen: set[str] = set()
            for session in sessions:
                meta = dict(session.meta or {})
                if meta.get('telegram_dialog_finished'):
                    continue
                username, _ = self._parse_tg(session.interview_url)
                if not username or username in seen:
                    continue
                seen.add(username)
                result.append(username)
            return result
        finally:
            db.close()

    async def _recent_inbound_cluster(self, entity, *, limit: int = 12) -> list[tuple[int, str]]:
        if self._client is None:
            return []
        messages = await self._client.get_messages(entity, limit=limit)
        cluster: list[tuple[int, str]] = []
        for message in messages:
            text = (getattr(message, 'message', '') or '').strip()
            if not text:
                continue
            if getattr(message, 'out', False):
                break
            cluster.append((int(message.id), text))
        cluster.reverse()
        return cluster

    # --------------------------------------------------------------- bootstrap

    async def _bootstrap_pending_sessions(self) -> None:
        if self._client is None:
            return
        db = SessionLocal()
        try:
            sessions = (
                db.query(InterviewSession)
                .filter(InterviewSession.state.in_(['new', 'active', 'waiting_approval']))
                .order_by(InterviewSession.updated_at.desc())
                .all()
            )
            chosen_by_bot: dict[str, InterviewSession] = {}
            for session in sessions:
                meta = dict(session.meta or {})
                if meta.get('telegram_dialog_finished'):
                    continue
                username, _ = self._parse_tg(session.interview_url)
                if not username:
                    continue
                existing = chosen_by_bot.get(username)
                if existing is None:
                    chosen_by_bot[username] = session
                    continue
                existing_meta = dict(existing.meta or {})
                if existing_meta.get('telegram_active_for_bot') == username and meta.get('telegram_active_for_bot') != username:
                    continue
                if meta.get('telegram_active_for_bot') == username and existing_meta.get('telegram_active_for_bot') != username:
                    chosen_by_bot[username] = session

            for username, session in chosen_by_bot.items():
                meta = dict(session.meta or {})
                if meta.get('telegram_bootstrap_sent'):
                    continue
                try:
                    entity = await self._client.get_entity(username)
                    if await self._recent_inbound_cluster(entity):
                        continue
                    _, payload = self._parse_tg(session.interview_url)
                    start_text = f'/start {payload}' if payload else '/start'
                    await self._client.send_message(entity, start_text)
                    self._mark_session_active(
                        db,
                        username,
                        session,
                        match_text=start_text,
                        match_score=999,
                        bootstrap_sent=True,
                    )
                    await asyncio.sleep(2)
                    self._log_info(
                        'telegram_bootstrap_sent',
                        f'Sent /start to @{username} for session {session.session_id}',
                    )
                except Exception as exc:
                    logger.exception('bootstrap failed for session %s', session.session_id)
                    self._log_error('telegram_bootstrap_failed', f'{session.session_id}: {exc}')
        finally:
            db.close()

    # --------------------------------------------------------------- handlers

    async def _on_new_message(self, event) -> None:
        try:
            sender = await event.get_sender()
            if sender is None or not getattr(sender, 'bot', False):
                return

            username = (getattr(sender, 'username', '') or '').lower()
            if not username:
                return

            message_text = (event.message.message or '').strip()
            if not message_text:
                return

            chat_key = self._event_chat_key(event, sender)
            dedupe_key = (chat_key, int(event.message.id))
            if dedupe_key in self._seen_message_keys:
                return
            self._seen_message_keys.add(dedupe_key)
            if len(self._seen_message_keys) > 4000:
                self._seen_message_keys = set(list(self._seen_message_keys)[-2000:])

            async with self._pending_lock:
                batch = self._pending_batches.get(chat_key)
                if batch is None:
                    batch = PendingChatBatch(username=username)
                    self._pending_batches[chat_key] = batch
                else:
                    batch.username = username
                    if batch.task and not batch.task.done():
                        batch.task.cancel()
                batch.events.append(event)
                batch.texts.append(message_text)
                batch.task = asyncio.create_task(
                    self._flush_pending_batch(chat_key),
                    name=f'telegram-batch-{chat_key}',
                )
        except Exception as exc:
            logger.exception('error in telegram on_new_message')
            self._log_error('telegram_listener_handler_error', str(exc))

    async def _flush_pending_batch(self, chat_key: int) -> None:
        try:
            await asyncio.sleep(BOT_REPLY_DEBOUNCE_SECONDS)
            async with self._pending_lock:
                batch = self._pending_batches.pop(chat_key, None)
            if batch is None or not batch.texts:
                return
            await self._process_batch_guarded(chat_key, batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('error while flushing telegram batch %s', chat_key)
            self._log_error('telegram_listener_batch_error', str(exc))

    async def _chat_is_busy(self, chat_key: int) -> bool:
        async with self._pending_lock:
            return chat_key in self._pending_batches or chat_key in self._processing_chat_keys

    async def _process_batch_guarded(self, chat_key: int, batch: PendingChatBatch) -> None:
        async with self._pending_lock:
            if chat_key in self._processing_chat_keys:
                return
            self._processing_chat_keys.add(chat_key)
        try:
            await self._process_pending_batch(chat_key, batch)
        finally:
            async with self._pending_lock:
                self._processing_chat_keys.discard(chat_key)

    async def _process_pending_batch(self, chat_key: int, batch: PendingChatBatch) -> None:
        current_session = self._find_session_for_bot(batch.username)
        actionable_messages: list[str] = []

        for message_text in batch.texts:
            if self._looks_like_dialog_finished(message_text) or self._looks_like_feedback_request(message_text):
                if current_session is not None:
                    self._complete_session(current_session, batch.username, message_text)
                    self._log_info(
                        'telegram_dialog_completed',
                        f'Session {current_session[1]} marked completed from @{batch.username}',
                    )
                current_session = None
                continue

            synced_session = self._sync_session_by_message(batch.username, message_text)
            if synced_session is not None:
                current_session = synced_session

            if self._is_non_actionable_status(message_text):
                continue

            actionable_messages.append(message_text)

        if not actionable_messages or current_session is None:
            return
        if self._auto_reply_paused():
            self._log_info(
                'telegram_auto_reply_paused',
                f'Auto-reply paused; skipped response for session {current_session[1]} in chat {chat_key}',
            )
            return

        combined_prompt = '\n\n'.join(item.strip() for item in actionable_messages if item.strip()).strip()
        if not combined_prompt:
            return

        self._emit_interactive_trace('question', batch.username, current_session[1], combined_prompt)
        answer = await self._generate_and_store(current_session, combined_prompt)
        if not answer:
            return

        if batch.events:
            await batch.events[-1].respond(answer)
        elif self._client is not None:
            await self._client.send_message(batch.username, answer)
        else:
            return

        self._emit_interactive_trace('answer', batch.username, current_session[1], answer)
        self._log_info(
            'telegram_auto_answer_sent',
            f'Auto-answered @{batch.username} in session {current_session[1]}',
        )

    # ------------------------------------------------------------- session map

    def _find_session_for_bot(self, username: str) -> tuple[int, str] | None:
        db = SessionLocal()
        try:
            candidates = self._session_candidates_for_bot(db, username)
            active = self._active_candidate(candidates, username)
            if active is not None:
                return (active.id, active.session_id)
            if len(candidates) == 1:
                return (candidates[0].id, candidates[0].session_id)
            if candidates:
                latest = candidates[0]
                return (latest.id, latest.session_id)
            return None
        finally:
            db.close()

    def _sync_session_by_message(self, username: str, message_text: str) -> tuple[int, str] | None:
        db = SessionLocal()
        try:
            candidates = self._session_candidates_for_bot(db, username)
            if not candidates:
                return None

            active = self._active_candidate(candidates, username)
            best, score = self._best_session_match(candidates, message_text)

            if best is None:
                if active is not None:
                    return (active.id, active.session_id)
                if len(candidates) == 1:
                    return (candidates[0].id, candidates[0].session_id)
                return None

            if active is not None and active.id == best.id:
                return (active.id, active.session_id)

            if score < SESSION_MATCH_THRESHOLD and active is not None:
                return (active.id, active.session_id)

            if score < SESSION_MATCH_THRESHOLD and len(candidates) > 1:
                return None

            self._mark_session_active(
                db,
                username,
                best,
                match_text=message_text,
                match_score=score,
            )
            return (best.id, best.session_id)
        finally:
            db.close()

    def _session_candidates_for_bot(self, db, username: str) -> list[InterviewSession]:
        sessions = (
            db.query(InterviewSession)
            .filter(InterviewSession.state != 'completed')
            .order_by(InterviewSession.updated_at.desc())
            .all()
        )
        return [session for session in sessions if self._parse_tg(session.interview_url)[0].lower() == username.lower()]

    def _active_candidate(self, candidates: list[InterviewSession], username: str) -> InterviewSession | None:
        for session in candidates:
            meta = dict(session.meta or {})
            if meta.get('telegram_active_for_bot') == username:
                return session
        return None

    def _best_session_match(
        self,
        candidates: list[InterviewSession],
        message_text: str,
    ) -> tuple[InterviewSession | None, int]:
        message_norm = self._normalize_text(message_text)
        message_tokens = self._meaningful_tokens(message_norm)
        best_session: InterviewSession | None = None
        best_score = 0

        for session in candidates:
            score = max(
                self._score_text_match(message_norm, message_tokens, session.vacancy_name),
                self._score_text_match(message_norm, message_tokens, session.subject),
            )
            if score > best_score:
                best_session = session
                best_score = score

        return best_session, best_score

    def _score_text_match(self, message_norm: str, message_tokens: set[str], candidate_text: str) -> int:
        candidate_norm = self._normalize_text(candidate_text)
        if not candidate_norm:
            return 0
        if candidate_norm in message_norm:
            return 100 + len(candidate_norm)

        candidate_tokens = self._meaningful_tokens(candidate_norm)
        if not candidate_tokens:
            return 0

        common_tokens = candidate_tokens & message_tokens
        if not common_tokens:
            return 0

        overlap_ratio = len(common_tokens) / len(candidate_tokens)
        longest_common = max(len(token) for token in common_tokens)
        score = int(overlap_ratio * 20) + len(common_tokens) * 6 + longest_common
        if len(common_tokens) >= 2:
            score += 8
        return score

    def _mark_session_active(
        self,
        db,
        username: str,
        target: InterviewSession,
        *,
        match_text: str,
        match_score: int,
        bootstrap_sent: bool = False,
    ) -> None:
        candidates = self._session_candidates_for_bot(db, username)
        for session in candidates:
            meta = dict(session.meta or {})
            if session.id == target.id:
                meta['telegram_active_for_bot'] = username
                meta['telegram_dialog_finished'] = False
                meta['telegram_last_bot_username'] = username
                meta['telegram_last_synced_at'] = datetime.utcnow().isoformat()
                meta['telegram_last_synced_score'] = match_score
                meta['telegram_last_synced_message'] = match_text[:500]
                if bootstrap_sent:
                    meta['telegram_bootstrap_sent'] = True
                    meta['telegram_bootstrap_auto'] = True
                session.meta = meta
                if session.state == 'new':
                    session.state = 'active'
            elif meta.get('telegram_active_for_bot') == username:
                meta.pop('telegram_active_for_bot', None)
                session.meta = meta
            db.add(session)
        db.commit()

    def _complete_session(self, session_ref: tuple[int, str], username: str, message_text: str) -> None:
        session_pk, _ = session_ref
        db = SessionLocal()
        try:
            session_row = db.query(InterviewSession).filter(InterviewSession.id == session_pk).first()
            if session_row is None:
                return
            meta = dict(session_row.meta or {})
            meta.pop('telegram_active_for_bot', None)
            meta['telegram_dialog_finished'] = True
            meta['telegram_last_bot_username'] = username
            meta['telegram_dialog_closed_at'] = datetime.utcnow().isoformat()
            meta['telegram_dialog_close_message'] = message_text[:500]
            session_row.meta = meta
            session_row.state = 'completed'
            db.add(session_row)
            db.commit()
        finally:
            db.close()

    # --------------------------------------------------------------- generate

    async def _generate_and_store(self, session_ref: tuple[int, str], question_text: str) -> str:
        session_pk, session_uuid = session_ref
        db = SessionLocal()
        try:
            session_row = db.query(InterviewSession).filter(InterviewSession.id == session_pk).first()
            if session_row is None:
                return ''

            qa = QuestionAnswer(
                session_id=session_row.id,
                question=question_text,
                status='draft',
                validation_status='draft',
            )
            session_row.state = 'active'
            db.add(qa)
            db.add(session_row)
            db.commit()
            db.refresh(qa)

            profile_model = self._profile.get_or_create_profile(db)
            profile_payload = self._profile.to_payload(profile_model)
            runtime_settings = self._settings.to_payload(self._settings.get_or_create(db))
        finally:
            db.close()

        try:
            answer_text, source = await self._llm.generate_answer(
                question_text,
                profile_payload,
                runtime_settings,
            )
        except Exception as exc:
            logger.exception('llm generate_answer failed')
            self._log_error('telegram_listener_llm_error', str(exc))
            return ''

        validation = self._validator.validate(answer_text, profile_payload)

        db = SessionLocal()
        try:
            qa_row = db.query(QuestionAnswer).filter(QuestionAnswer.id == qa.id).first()
            if qa_row is None:
                return ''
            qa_row.draft_answer = answer_text
            qa_row.final_answer = answer_text
            qa_row.validation_status = validation.status
            qa_row.status = 'approved'
            meta = dict(qa_row.meta or {})
            meta.update(
                {
                    'warnings': validation.warnings,
                    'source': source,
                    'auto_sent': True,
                    'channel': 'telegram_listener',
                }
            )
            qa_row.meta = meta

            session_row = db.query(InterviewSession).filter(InterviewSession.id == session_pk).first()
            if session_row is not None:
                session_row.state = 'active'
                db.add(session_row)
            db.add(qa_row)
            db.commit()

            LogService.write(
                db,
                LogEventPayload(
                    event_type='telegram_auto_answer',
                    message='Auto-generated answer for inbound telegram question',
                    payload={
                        'session_id': session_uuid,
                        'question_id': qa_row.id,
                        'source': source,
                        'validation': validation.status,
                    },
                ),
            )
        finally:
            db.close()
        return answer_text

    # ----------------------------------------------------------------- utils

    @staticmethod
    def _event_chat_key(event, sender) -> int:
        chat_id = getattr(event, 'chat_id', None)
        if chat_id is not None:
            return int(chat_id)
        sender_id = getattr(sender, 'id', None)
        return int(sender_id or 0)

    @staticmethod
    def _normalize_text(value: str) -> str:
        lowered = (value or '').lower().replace('ё', 'е')
        cleaned = re.sub(r'[^0-9a-zа-я]+', ' ', lowered)
        return ' '.join(cleaned.split())

    @classmethod
    def _meaningful_tokens(cls, value: str) -> set[str]:
        return {token for token in value.split() if len(token) >= 3}

    @classmethod
    def _looks_like_dialog_finished(cls, message_text: str) -> bool:
        normalized = cls._normalize_text(message_text)
        if not normalized:
            return False
        if 'диалог по вакансии' in normalized and ('заверш' in normalized or 'закончен' in normalized):
            return True
        return any(marker in normalized for marker in FINAL_MESSAGE_MARKERS)

    @classmethod
    def _looks_like_feedback_request(cls, message_text: str) -> bool:
        normalized = cls._normalize_text(message_text)
        if not normalized:
            return False
        return any(marker in normalized for marker in FEEDBACK_MESSAGE_MARKERS)

    @classmethod
    def _is_actionable_prompt(cls, message_text: str) -> bool:
        normalized = cls._normalize_text(message_text)
        if not normalized:
            return False
        if '?' in message_text:
            return True
        return any(marker in normalized for marker in PROMPT_MARKERS)

    @classmethod
    def _is_non_actionable_status(cls, message_text: str) -> bool:
        normalized = cls._normalize_text(message_text)
        if not normalized:
            return True
        if cls._looks_like_dialog_finished(message_text) or cls._looks_like_feedback_request(message_text):
            return True
        if cls._is_actionable_prompt(message_text):
            return False
        return any(marker in normalized for marker in STATUS_MESSAGE_MARKERS)

    def _auto_reply_paused(self) -> bool:
        db = SessionLocal()
        try:
            entry = self._settings.get_or_create(db)
            return entry.telegram_auto_reply_paused == 'yes'
        finally:
            db.close()

    @staticmethod
    def _parse_tg(url: str) -> tuple[str, str]:
        parsed = urlparse(url or '')
        if parsed.netloc.lower() not in {'t.me', 'telegram.me', 'www.t.me'}:
            return '', ''
        username = parsed.path.strip('/').split('/')[0]
        query = parse_qs(parsed.query)
        payload = ''
        for key in ('start', 'startapp', 'startattach'):
            if query.get(key):
                payload = query[key][0]
                break
        return username, payload

    def _emit_interactive_trace(self, kind: str, username: str, session_id: str, text: str) -> None:
        if os.getenv('GIH_INTERACTIVE_LOG', '').strip().lower() not in {'1', 'true', 'yes', 'on'}:
            return
        print(f'[telegram:{kind}][@{username}][{session_id}] {text}', flush=True)

    def _log_info(self, event_type: str, message: str) -> None:
        db = SessionLocal()
        try:
            LogService.write(
                db,
                LogEventPayload(level='info', event_type=event_type, message=message, payload={}),
            )
        except Exception:
            pass
        finally:
            db.close()

    def _log_error(self, event_type: str, message: str) -> None:
        db = SessionLocal()
        try:
            LogService.write(
                db,
                LogEventPayload(level='error', event_type=event_type, message=message, payload={}),
            )
        except Exception:
            pass
        finally:
            db.close()
