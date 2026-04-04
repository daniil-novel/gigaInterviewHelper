import asyncio
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func
from sqlalchemy.orm import Session, selectinload

from app.database import Base, SessionLocal, engine, ensure_sqlite_migrations, get_db
from app.models import CandidateProfile, EmailLog, InterviewSession, QuestionAnswer
from app.schemas import (
    AnswerActionPayload,
    CandidateProfilePayload,
    DashboardStats,
    EmailIngestPayload,
    GenerateAnswerResponse,
    LogEventPayload,
    MailboxSettingsPayload,
    OpenRouterSettingsPayload,
    QuestionCreatePayload,
    SessionCreatePayload,
    TelegramCodeRequestPayload,
    TelegramCodeVerifyPayload,
    TelegramSettingsPayload,
)
from app.services.email_service import EmailService
from app.services.gmail_oauth_service import GmailAPIError, GmailOAuthService
from app.services.llm_service import LLMService
from app.services.logging_service import LogService
from app.services.mailbox_automation_service import MailboxAutomationService
from app.services.profile_service import ProfileService
from app.services.session_service import SessionService
from app.services.settings_service import SettingsService
from app.services.telegram_automation_service import TelegramAutomationService
from app.services.validator_service import AnswerValidator

BASE_DIR = Path(__file__).resolve().parent
Base.metadata.create_all(bind=engine)
ensure_sqlite_migrations()

app = FastAPI(title='gigaInterviewHelper')
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')
templates = Jinja2Templates(directory=str(BASE_DIR / 'templates'))

email_service = EmailService()
profile_service = ProfileService()
session_service = SessionService()
llm_service = LLMService()
validator = AnswerValidator()
settings_service = SettingsService()
mailbox_automation_service = MailboxAutomationService()
telegram_automation_service = TelegramAutomationService()
gmail_oauth_service = GmailOAuthService()


@app.on_event('startup')
async def startup_event() -> None:
    app.state.mailbox_poll_task = asyncio.create_task(mailbox_poll_loop())


@app.on_event('shutdown')
async def shutdown_event() -> None:
    task = getattr(app.state, 'mailbox_poll_task', None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def mailbox_poll_loop() -> None:
    while True:
        sleep_for = 60
        try:
            db = SessionLocal()
            app_settings = settings_service.get_or_create(db)
            sleep_for = max(15, app_settings.mail_poll_interval_seconds or 60)
            db.close()
            if app_settings.mail_poll_enabled == 'yes':
                await asyncio.to_thread(_run_mailbox_sync_once)
        except Exception as exc:
            db = SessionLocal()
            with suppress(Exception):
                LogService.write(
                    db,
                    LogEventPayload(
                        level='error',
                        event_type='mailbox_poll_error',
                        message='Mailbox poll failed',
                        payload={'error': str(exc)},
                    ),
                )
            db.close()
        finally:
            with suppress(Exception):
                db.close()
        await asyncio.sleep(sleep_for)


@app.get('/', response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    profile = profile_service.get_or_create_profile(db)
    app_settings = settings_service.get_or_create(db)
    sessions = (
        db.query(InterviewSession)
        .options(selectinload(InterviewSession.questions))
        .order_by(desc(InterviewSession.updated_at))
        .all()
    )
    emails = db.query(EmailLog).order_by(desc(EmailLog.received_at)).limit(10).all()
    logs = db.query(QuestionAnswer).order_by(desc(QuestionAnswer.updated_at)).limit(20).all()
    stats = DashboardStats(
        sessions_total=db.query(func.count(InterviewSession.id)).scalar() or 0,
        sessions_waiting_approval=db.query(func.count(InterviewSession.id)).filter(InterviewSession.state == 'waiting_approval').scalar() or 0,
        questions_total=db.query(func.count(QuestionAnswer.id)).scalar() or 0,
        emails_total=db.query(func.count(EmailLog.id)).scalar() or 0,
        last_resume_update=profile.updated_at,
    )
    return templates.TemplateResponse(
        'dashboard.html',
        {
            'request': request,
            'profile': profile,
            'sessions': sessions,
            'emails': emails,
            'question_logs': logs,
            'stats': stats,
            'app_settings': app_settings,
            'openrouter_settings': app_settings,
            'masked_openrouter_key': settings_service.masked_key(app_settings.openrouter_api_key),
            'masked_imap_password': settings_service.masked_secret(app_settings.imap_password),
            'masked_gmail_client_secret': settings_service.masked_secret(app_settings.gmail_oauth_client_secret),
            'has_gmail_refresh_token': bool(app_settings.gmail_oauth_refresh_token),
            'masked_telegram_hash': settings_service.masked_secret(app_settings.telegram_api_hash),
            'has_telegram_session': bool(app_settings.telegram_session_string),
            'ui_notice': request.query_params.get('notice', ''),
            'ui_notice_type': request.query_params.get('notice_type', ''),
            'ui_notice_link': request.query_params.get('notice_link', ''),
        },
    )


@app.post('/api/email-ingest')
def ingest_email(payload: EmailIngestPayload, db: Session = Depends(get_db)):
    parsed = email_service.parse_invite(payload)
    session_service.log_email(db, parsed, payload.html_body, payload.text_body)
    session = None
    if parsed.matched:
        session = session_service.create_or_get_session(
            db,
            SessionCreatePayload(
                vacancy_name=parsed.vacancy_name,
                interview_url=parsed.interview_url,
                source_email=parsed.source_email,
                subject=parsed.subject,
            ),
        )
    LogService.write(
        db,
        LogEventPayload(
            event_type='email_ingest',
            message='Processed incoming email',
            payload={'matched': parsed.matched, 'interview_url': parsed.interview_url},
        ),
    )
    return {'parsed': parsed.model_dump(mode='json'), 'session_id': session.session_id if session else None}


@app.post('/api/sessions')
def create_session(payload: SessionCreatePayload, db: Session = Depends(get_db)):
    session = session_service.create_or_get_session(db, payload)
    return {'session_id': session.session_id, 'state': session.state}


@app.post('/api/profile')
def save_profile(payload: CandidateProfilePayload, db: Session = Depends(get_db)):
    profile = profile_service.update_profile(db, payload)
    LogService.write(db, LogEventPayload(event_type='profile_update', message='Profile updated', payload={'profile_id': profile.id}))
    return {'status': 'ok', 'profile_id': profile.id}


@app.post('/api/settings/openrouter')
def save_openrouter_settings(payload: OpenRouterSettingsPayload, db: Session = Depends(get_db)):
    settings = settings_service.update_openrouter(db, payload)
    LogService.write(db, LogEventPayload(event_type='settings_update', message='OpenRouter settings updated', payload={'model': settings.openrouter_model}))
    return {'status': 'ok', 'model': settings.openrouter_model}


@app.post('/api/settings/mailbox')
def save_mailbox_settings(payload: MailboxSettingsPayload, db: Session = Depends(get_db)):
    settings = settings_service.update_mailbox(db, payload)
    LogService.write(db, LogEventPayload(event_type='mailbox_settings_update', message='Mailbox settings updated', payload={'email': settings.personal_email}))
    return {'status': 'ok', 'email': settings.personal_email, 'poll_enabled': settings.mail_poll_enabled}


@app.get('/auth/gmail/start')
def gmail_oauth_start(db: Session = Depends(get_db)):
    entry = settings_service.get_or_create(db)
    try:
        auth_url, state = gmail_oauth_service.build_authorization_url(entry)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f'Gmail OAuth is not configured: {exc}') from exc
    entry.gmail_oauth_state = state
    entry.gmail_oauth_status = 'authorization_started'
    settings_service.save_runtime(db, entry)
    return RedirectResponse(auth_url, status_code=302)


@app.get('/auth/gmail/callback')
async def gmail_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    entry = settings_service.get_or_create(db)
    if error:
        entry.gmail_oauth_status = f'error:{error}'
        settings_service.save_runtime(db, entry)
        return _redirect_with_notice(f'Google OAuth returned error: {error}', notice_type='error')
    if not code or not state or state != entry.gmail_oauth_state:
        return _redirect_with_notice('Invalid Gmail OAuth callback state. Try connecting Gmail again.', notice_type='error')
    try:
        token_data = await gmail_oauth_service.exchange_code(entry, code)
    except Exception as exc:
        entry.gmail_oauth_status = 'exchange_failed'
        settings_service.save_runtime(db, entry)
        return _redirect_with_notice(f'Gmail OAuth token exchange failed: {exc}', notice_type='error')
    entry.gmail_oauth_access_token = token_data.get('access_token', '')
    if token_data.get('refresh_token'):
        entry.gmail_oauth_refresh_token = token_data['refresh_token']
    entry.gmail_oauth_state = ''
    entry.gmail_oauth_status = 'connected' if entry.gmail_oauth_refresh_token else 'missing_refresh_token'
    settings_service.save_runtime(db, entry)
    if entry.gmail_oauth_status == 'connected':
        return _redirect_with_notice('Gmail OAuth connected. Now click "Проверить почту сейчас".', notice_type='success')
    return _redirect_with_notice('Gmail connected, but refresh token is missing. Reconnect Gmail and make sure consent is granted.', notice_type='error')


@app.post('/api/settings/telegram')
def save_telegram_settings(payload: TelegramSettingsPayload, db: Session = Depends(get_db)):
    settings = settings_service.update_telegram(db, payload)
    LogService.write(db, LogEventPayload(event_type='telegram_settings_update', message='Telegram settings updated', payload={'phone': settings.telegram_phone_number}))
    return {'status': 'ok', 'phone': settings.telegram_phone_number, 'auto_send_telegram': settings.auto_send_telegram}


@app.post('/api/mailbox/sync')
def sync_mailbox(db: Session = Depends(get_db)):
    try:
        result = mailbox_automation_service.sync_latest_messages(db)
    except GmailAPIError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                'message': str(exc),
                'reason': exc.reason,
                'activation_url': exc.activation_url,
                'status_code': exc.status_code,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Mailbox sync failed: {exc}') from exc
    return result


@app.get('/api/mailbox/diagnostics')
async def mailbox_diagnostics(db: Session = Depends(get_db)):
    entry = settings_service.get_or_create(db)
    if entry.mail_provider != 'gmail_oauth':
        return {'provider': entry.mail_provider, 'status': 'no_gmail_oauth'}
    try:
        result = await gmail_oauth_service.diagnose(entry)
        settings_service.save_runtime(db, entry)
        return result
    except GmailAPIError as exc:
        settings_service.save_runtime(db, entry)
        raise HTTPException(
            status_code=502,
            detail={
                'message': str(exc),
                'reason': exc.reason,
                'activation_url': exc.activation_url,
                'status_code': exc.status_code,
            },
        ) from exc


@app.get('/mailbox/diagnostics-view')
async def mailbox_diagnostics_view(db: Session = Depends(get_db)):
    entry = settings_service.get_or_create(db)
    if entry.mail_provider != 'gmail_oauth':
        return _redirect_with_notice('Почта сейчас работает не через Gmail OAuth. Для Gmail-диагностики выбери провайдер Gmail OAuth.', notice_type='info')
    try:
        result = await gmail_oauth_service.diagnose(entry)
        settings_service.save_runtime(db, entry)
        return _redirect_with_notice('Gmail OAuth настроен корректно. Можно проверять почту.', notice_type='success')
    except GmailAPIError as exc:
        settings_service.save_runtime(db, entry)
        return _redirect_with_notice(str(exc), notice_type='error', notice_link=exc.activation_url)


@app.post('/api/telegram/request-code')
async def telegram_request_code(payload: TelegramCodeRequestPayload, db: Session = Depends(get_db)):
    entry = settings_service.get_or_create(db)
    entry.telegram_api_id = payload.telegram_api_id.strip() or entry.telegram_api_id
    entry.telegram_api_hash = payload.telegram_api_hash.strip() or entry.telegram_api_hash
    entry.telegram_phone_number = payload.telegram_phone_number.strip() or entry.telegram_phone_number
    try:
        await telegram_automation_service.request_login_code(entry)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Telegram code request failed: {exc}') from exc
    settings_service.save_telegram_runtime(db, entry)
    return {'status': 'ok', 'telegram_auth_status': entry.telegram_auth_status}


@app.post('/api/telegram/verify-code')
async def telegram_verify_code(payload: TelegramCodeVerifyPayload, db: Session = Depends(get_db)):
    entry = settings_service.get_or_create(db)
    try:
        await telegram_automation_service.verify_login_code(entry, payload.code.strip(), payload.telegram_2fa_password.strip() or entry.telegram_2fa_password)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Telegram verify failed: {exc}') from exc
    settings_service.save_telegram_runtime(db, entry)
    return {'status': 'ok', 'telegram_auth_status': entry.telegram_auth_status}


@app.post('/api/profile/import-resume')
async def import_resume(file: UploadFile = File(...), db: Session = Depends(get_db)):
    runtime_settings = settings_service.to_payload(settings_service.get_or_create(db))
    resume_path = await profile_service.store_resume_file(file)
    try:
        parsed_profile = await llm_service.parse_resume_pdf(resume_path, runtime_settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'OpenRouter resume parsing failed: {exc}') from exc
    profile = profile_service.update_profile(db, parsed_profile)
    LogService.write(
        db,
        LogEventPayload(
            event_type='resume_import',
            message='Resume imported and parsed with OpenRouter',
            payload={'file': file.filename, 'model': runtime_settings.openrouter_model},
        ),
    )
    return {'status': 'ok', 'profile_id': profile.id}


@app.post('/api/sessions/{session_id}/questions', response_model=GenerateAnswerResponse)
async def add_question(session_id: str, payload: QuestionCreatePayload, db: Session = Depends(get_db)):
    session = db.query(InterviewSession).filter(InterviewSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail='Session not found')

    profile_model = profile_service.get_or_create_profile(db)
    profile_payload = profile_service.to_payload(profile_model)
    runtime_settings = settings_service.to_payload(settings_service.get_or_create(db))
    entry = session_service.add_question(db, session, payload.question)
    try:
        answer, source = await llm_service.generate_answer(payload.question, profile_payload, runtime_settings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Answer generation failed: {exc}') from exc
    validation = validator.validate(answer, profile_payload)

    entry.draft_answer = answer
    entry.validation_status = validation.status
    entry.status = 'draft'
    entry.meta = {'warnings': validation.warnings, 'source': source}
    session.state = 'waiting_approval'
    db.add_all([entry, session])
    db.commit()
    db.refresh(entry)

    LogService.write(
        db,
        LogEventPayload(
            event_type='answer_generated',
            message='Draft answer generated',
            payload={'session_id': session.session_id, 'question_id': entry.id, 'source': source},
        ),
    )
    return GenerateAnswerResponse(
        question_id=entry.id,
        draft_answer=entry.draft_answer,
        validation_status=entry.validation_status,
        needs_review=validation.needs_review,
        warnings=validation.warnings,
        source=source,
    )


@app.post('/api/questions/{question_id}/actions')
async def handle_answer_action(question_id: int, payload: AnswerActionPayload, db: Session = Depends(get_db)):
    question = db.query(QuestionAnswer).filter(QuestionAnswer.id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail='Question not found')

    if payload.action == 'approve':
        question.final_answer = question.final_answer or question.draft_answer
        question.status = 'approved'
        question.session.state = 'completed'
    elif payload.action == 'edit':
        question.final_answer = payload.final_answer.strip()
        question.status = 'approved'
        question.session.state = 'completed'
    elif payload.action == 'skip':
        question.status = 'rejected'
        question.session.state = 'active'

    delivery_meta = dict(question.meta or {})
    settings_entry = settings_service.get_or_create(db)
    if payload.action in {'approve', 'edit'} and settings_entry.auto_send_telegram == 'yes' and settings_entry.telegram_auth_status == 'authorized' and settings_entry.telegram_session_string:
        try:
            message_id = await telegram_automation_service.send_interview_answer(settings_entry, question.session, question.final_answer)
            delivery_meta['telegram_delivery'] = {'status': 'sent', 'message_id': message_id}
        except Exception as exc:
            delivery_meta['telegram_delivery'] = {'status': 'failed', 'error': str(exc)}
    elif payload.action in {'approve', 'edit'} and settings_entry.auto_send_telegram == 'yes':
        delivery_meta['telegram_delivery'] = {'status': 'skipped', 'error': 'Advanced Telegram is not authorized. Manual browser flow remains available.'}
    question.meta = delivery_meta

    db.add(question)
    db.add(question.session)
    db.add(settings_entry)
    db.commit()
    LogService.write(db, LogEventPayload(event_type='answer_action', message='Answer action applied', payload={'question_id': question.id, 'action': payload.action, 'telegram_delivery': delivery_meta.get('telegram_delivery', {})}))
    return {'status': 'ok', 'telegram_delivery': delivery_meta.get('telegram_delivery')}


@app.post('/api/questions/{question_id}/regenerate', response_model=GenerateAnswerResponse)
async def regenerate_answer(question_id: int, db: Session = Depends(get_db)):
    question = db.query(QuestionAnswer).filter(QuestionAnswer.id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail='Question not found')

    profile_model = profile_service.get_or_create_profile(db)
    profile_payload = profile_service.to_payload(profile_model)
    runtime_settings = settings_service.to_payload(settings_service.get_or_create(db))
    try:
        answer, source = await llm_service.generate_answer(question.question, profile_payload, runtime_settings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Answer regeneration failed: {exc}') from exc
    validation = validator.validate(answer, profile_payload)

    question.draft_answer = answer
    question.validation_status = validation.status
    question.status = 'draft'
    question.meta = {'warnings': validation.warnings, 'source': source}
    question.session.state = 'waiting_approval'
    db.add(question)
    db.add(question.session)
    db.commit()
    db.refresh(question)

    LogService.write(
        db,
        LogEventPayload(
            event_type='answer_regenerated',
            message='Draft answer regenerated',
            payload={'question_id': question.id, 'source': source},
        ),
    )
    return GenerateAnswerResponse(
        question_id=question.id,
        draft_answer=question.draft_answer,
        validation_status=question.validation_status,
        needs_review=validation.needs_review,
        warnings=validation.warnings,
        source=source,
    )


@app.post('/sessions/create-from-form')
def create_session_form(
    vacancy_name: str = Form(...),
    interview_url: str = Form(...),
    source_email: str = Form('manual@local'),
    subject: str = Form('Manual session'),
    db: Session = Depends(get_db),
):
    session_service.create_or_get_session(
        db,
        SessionCreatePayload(vacancy_name=vacancy_name, interview_url=interview_url, source_email=source_email, subject=subject),
    )
    return RedirectResponse('/', status_code=303)


@app.post('/profile/update-form')
def update_profile_form(
    full_name: str = Form(''),
    current_role: str = Form(''),
    experience_summary: str = Form(''),
    skills: str = Form(''),
    projects: str = Form(''),
    education: str = Form(''),
    achievements: str = Form(''),
    english_level: str = Form(''),
    salary_expectation: str = Form(''),
    work_format: str = Form(''),
    notice_period: str = Form(''),
    must_not_claim: str = Form(''),
    db: Session = Depends(get_db),
):
    payload = CandidateProfilePayload(
        full_name=full_name,
        current_role=current_role,
        experience_summary=experience_summary,
        skills=_split_lines(skills),
        projects=_split_lines(projects),
        education=_split_lines(education),
        achievements=_split_lines(achievements),
        english_level=english_level,
        salary_expectation=salary_expectation,
        work_format=work_format,
        notice_period=notice_period,
        must_not_claim=_split_lines(must_not_claim),
    )
    profile_service.update_profile(db, payload)
    return RedirectResponse('/', status_code=303)


@app.post('/profile/import-form')
async def import_profile_form(file: UploadFile = File(...), db: Session = Depends(get_db)):
    await import_resume(file, db)
    return RedirectResponse('/', status_code=303)


@app.post('/settings/openrouter-form')
def save_openrouter_settings_form(
    openrouter_api_key: str = Form(''),
    openrouter_model: str = Form('google/gemini-3.1-flash-lite-preview'),
    pdf_engine: str = Form('pdf-text'),
    db: Session = Depends(get_db),
):
    save_openrouter_settings(
        OpenRouterSettingsPayload(
            openrouter_api_key=openrouter_api_key,
            openrouter_model=openrouter_model,
            pdf_engine=pdf_engine,
        ),
        db,
    )
    return RedirectResponse('/', status_code=303)


@app.post('/settings/mailbox-form')
def save_mailbox_settings_form(
    mail_provider: str = Form('gmail_oauth'),
    personal_email: str = Form(''),
    gmail_oauth_client_id: str = Form(''),
    gmail_oauth_client_secret: str = Form(''),
    gmail_oauth_redirect_uri: str = Form(''),
    imap_host: str = Form(''),
    imap_port: int = Form(993),
    imap_password: str = Form(''),
    imap_folder: str = Form('INBOX'),
    mail_poll_enabled: str = Form('no'),
    mail_poll_interval_seconds: int = Form(60),
    db: Session = Depends(get_db),
):
    save_mailbox_settings(
        MailboxSettingsPayload(
            mail_provider=mail_provider,
            personal_email=personal_email,
            gmail_oauth_client_id=gmail_oauth_client_id,
            gmail_oauth_client_secret=gmail_oauth_client_secret,
            gmail_oauth_redirect_uri=gmail_oauth_redirect_uri,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_password=imap_password,
            imap_folder=imap_folder,
            mail_poll_enabled=mail_poll_enabled == 'yes',
            mail_poll_interval_seconds=mail_poll_interval_seconds,
        ),
        db,
    )
    return RedirectResponse('/', status_code=303)


@app.post('/gmail/oauth/start-form')
def gmail_oauth_start_form(db: Session = Depends(get_db)):
    return gmail_oauth_start(db)


@app.post('/mailbox/sync-form')
def sync_mailbox_form(db: Session = Depends(get_db)):
    try:
        result = sync_mailbox(db)
        provider = result.get('provider', 'mailbox')
        processed = result.get('processed', 0)
        created_sessions = result.get('created_sessions', 0)
        return _redirect_with_notice(
            f'Почта проверена через {provider}. Обработано писем: {processed}. Новых сессий: {created_sessions}.',
            notice_type='success',
        )
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            return _redirect_with_notice(detail.get('message', 'Mailbox sync failed'), notice_type='error', notice_link=detail.get('activation_url', ''))
        return _redirect_with_notice(str(detail), notice_type='error')


@app.post('/settings/telegram-form')
def save_telegram_settings_form(
    telegram_api_id: str = Form(''),
    telegram_api_hash: str = Form(''),
    telegram_phone_number: str = Form(''),
    telegram_2fa_password: str = Form(''),
    auto_send_telegram: str = Form('no'),
    db: Session = Depends(get_db),
):
    save_telegram_settings(
        TelegramSettingsPayload(
            telegram_api_id=telegram_api_id,
            telegram_api_hash=telegram_api_hash,
            telegram_phone_number=telegram_phone_number,
            telegram_2fa_password=telegram_2fa_password,
            auto_send_telegram=auto_send_telegram == 'yes',
        ),
        db,
    )
    return RedirectResponse('/', status_code=303)


@app.post('/telegram/request-code-form')
async def telegram_request_code_form(db: Session = Depends(get_db)):
    settings = settings_service.get_or_create(db)
    await telegram_request_code(
        TelegramCodeRequestPayload(
            telegram_api_id=settings.telegram_api_id,
            telegram_api_hash=settings.telegram_api_hash,
            telegram_phone_number=settings.telegram_phone_number,
        ),
        db,
    )
    return RedirectResponse('/', status_code=303)


@app.post('/telegram/verify-code-form')
async def telegram_verify_code_form(code: str = Form(...), telegram_2fa_password: str = Form(''), db: Session = Depends(get_db)):
    await telegram_verify_code(TelegramCodeVerifyPayload(code=code, telegram_2fa_password=telegram_2fa_password), db)
    return RedirectResponse('/', status_code=303)


@app.post('/sessions/{session_id}/ask-form')
async def ask_question_form(session_id: str, question: str = Form(...), db: Session = Depends(get_db)):
    await add_question(session_id, QuestionCreatePayload(question=question), db)
    return RedirectResponse('/', status_code=303)


@app.post('/questions/{question_id}/action-form')
async def answer_action_form(
    question_id: int,
    action: str = Form(...),
    final_answer: str = Form(''),
    db: Session = Depends(get_db),
):
    await handle_answer_action(question_id, AnswerActionPayload(action=action, final_answer=final_answer), db)
    return RedirectResponse('/', status_code=303)


@app.post('/questions/{question_id}/regenerate-form')
async def regenerate_answer_form(question_id: int, db: Session = Depends(get_db)):
    await regenerate_answer(question_id, db)
    return RedirectResponse('/', status_code=303)


@app.get('/health')
def healthcheck(db: Session = Depends(get_db)):
    profile_exists = db.query(CandidateProfile).count() > 0
    return {'status': 'ok', 'profile_exists': profile_exists}


def _split_lines(value: str) -> list[str]:
    return [item.strip() for item in value.replace('\r', '').split('\n') if item.strip()]


def _run_mailbox_sync_once() -> None:
    db = SessionLocal()
    try:
        mailbox_automation_service.sync_latest_messages(db)
    finally:
        db.close()


def _redirect_with_notice(message: str, *, notice_type: str = 'info', notice_link: str = '') -> RedirectResponse:
    query = {'notice': message, 'notice_type': notice_type}
    if notice_link:
        query['notice_link'] = notice_link
    return RedirectResponse(f"/?{urlencode(query)}", status_code=303)
