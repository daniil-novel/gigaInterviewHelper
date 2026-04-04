# gigaInterviewHelper

MVP из ТЗ: FastAPI + SQLite + web-панель для автоматизированной подготовки ответов на AI-интервью.

## Что уже реализовано

- приём писем через `POST /api/email-ingest`;
- автоопрос почты;
- полноценный Gmail OAuth 2.0 через authorization code flow и Gmail API;
- IMAP fallback для не-Gmail ящиков;
- фильтрация писем от `noreply@hh.ru` и `hrplatform@sberbank.ru`;
- извлечение вакансии и ссылки на интервью из письма;
- хранение email-логов, сессий, вопросов и ответов в SQLite;
- импорт `resume.pdf` напрямую в OpenRouter как PDF-файла;
- LLM-парсинг резюме в профиль кандидата;
- Telegram user API: хранение `api_id`, `api_hash`, телефона, отправка кода, подтверждение кода, хранение session string;
- автоматическая отправка подтверждённого ответа в Telegram, если ссылка интервью ведёт на `t.me`/`telegram.me`.

## Запуск

```bash
python -m venv .venv
.\.venv\Scripts\python -m ensurepip --upgrade
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

Открыть: `http://127.0.0.1:8000`

## Gmail OAuth: как настроить

Нужен OAuth client из Google Cloud Console.

### 1. Создай OAuth client

В Google Cloud:
- включи Gmail API;
- настрой OAuth consent screen;
- создай `OAuth client ID` типа `Web application`.

### 2. Добавь redirect URI

Для локального запуска добавь в Google Cloud этот redirect URI:

```text
http://127.0.0.1:8000/auth/gmail/callback
```

Если запускаешь на другом хосте или порту, укажи его точно в таком же виде и поставь тот же URI в UI приложения.

### 3. Заполни в UI блок `Личная почта`

- `Провайдер почты` = `Gmail OAuth`
- `Личный email`
- `Google OAuth Client ID`
- `Google OAuth Client Secret`
- `Redirect URI`
- `Включить автоопрос`

Нажми `Сохранить почту`, затем `Подключить Gmail`.

### 4. Пройди Google consent

После согласия Google вернёт приложение на `/auth/gmail/callback`, а refresh token сохранится в базе.

### 5. Проверь синхронизацию

Нажми `Проверить почту сейчас`.

Если увидишь ошибку про `Gmail API is disabled in Google Cloud project`, это значит, что OAuth уже работает, но в самом Google Cloud проекте ещё не включён `Gmail API`.

В этом случае:
- открой `APIs & Services`
- найди `Gmail API`
- нажми `Enable`
- подожди 1-5 минут
- повтори проверку почты

Для быстрой диагностики в UI есть кнопка `Проверить Gmail OAuth`.

## IMAP fallback

Если почта не Gmail, можно переключить `Провайдер почты` на `IMAP` и указать host/port/password.

## Основной сценарий работы

1. Подключаешь Gmail.
2. Приложение находит письмо-приглашение и создаёт сессию.
3. Нажимаешь `Открыть интервью`.
4. Интервью открывается в браузере.
5. Когда видишь вопрос, вставляешь его в приложение.
6. Получаешь готовый ответ.
7. Вставляешь ответ вручную в браузере.

Это теперь главный и рекомендуемый сценарий.

## Telegram

### Простой режим

Ничего подключать не нужно.

Сценарий такой:
- приложение генерирует ответ;
- ты нажимаешь `Открыть интервью`;
- открывается ссылка AI-интервью в браузере;
- ты вставляешь готовый ответ вручную.

Это основной и рекомендуемый вариант, если не хочется разбираться с Telegram API.

### Advanced режим

Нужен только если приложение должно отправлять ответы в Telegram само от имени твоего аккаунта.

Для этого нужен не `user id`, а именно:
- `API ID`
- `API Hash`

Их получают на `https://my.telegram.org` в разделе `API development tools`.

После этого в UI:
- сохрани Telegram settings;
- нажми `Отправить код в Telegram`;
- введи код;
- при необходимости введи 2FA password.

Важно:
- `Ваш ID` в Telegram не подходит для этого режима;
- простого OAuth-аналога для отправки сообщений от имени пользователя у Telegram нет.

## Ограничения

- Для Gmail нужен собственный Google OAuth client.
- Для Telegram нужен собственный `api_id/api_hash`.
- Секреты пока хранятся в SQLite в открытом виде, для локального MVP это допустимо, для продакшена нет.
