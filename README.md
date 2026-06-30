# Telegram Work Summary Bot

Telegram Work Summary Bot — это внутренний инструмент для сбора, архивации и подготовки рабочих Telegram-чатов к AI-анализу.

Бот работает как тихий архиватор: он сохраняет сообщения и вложения из подтверждённых чатов, а затем по запросу администратора собирает структурированный пакет для анализа в ChatGPT. При включённой настройке он также может автоматически расшифровывать Telegram voice/audio через OpenAI speech-to-text API во время сборки рабочего пакета.

## Основная идея

Рабочий Telegram-чат быстро превращается в поток сообщений, файлов, скриншотов, голосовых, документов и договорённостей. Через несколько дней бывает сложно восстановить, кто что обещал, какие задачи появились, какие вопросы остались открытыми и какие файлы были важными.

Бот решает эту задачу через отдельный pipeline:

- сохраняет сообщения и вложения из подтверждённых чатов;
- связывает файлы с контекстом сообщений;
- готовит Markdown-экспорт переписки;
- создаёт индекс вложений;
- собирает OCR-friendly contact sheets для изображений и скриншотов;
- добавляет автоматические transcript для voice/audio, если включена OpenAI-транскрибация;
- формирует полный ZIP с оригинальными файлами;
- добавляет prompt для анализа в ChatGPT;
- позволяет получить рабочую сводку, список задач, рисков и следующих шагов.

## Основной сценарий работы

1. Бот добавляется в рабочий Telegram-чат.
2. Администратор подтверждает чат.
3. Бот молча сохраняет сообщения и файлы.
4. Администратор открывает личный чат с ботом.
5. Командой `/report_chats` выбирает нужный чат и период.
6. Бот присылает AI-ready пакет.
7. Пакет загружается в ChatGPT для анализа.

Рабочий чат при этом не засоряется служебными файлами и командами.

## Что входит в рабочий пакет

### `01_chat_export.md`

Markdown-файл с полной перепиской за выбранный период. В нём сохраняются дата и время сообщения, автор, тип сообщения, текст, подписи к файлам и информация о вложениях.

### `02_attachments_index.md`

Индекс вложений. Для каждого файла бот фиксирует дату и время, автора, тип файла, имя файла, размер, путь внутри архива, подпись и несколько сообщений до и после файла.

Это помогает модели понять не только сам факт наличия файла, но и рабочий контекст вокруг него.

### `03_work_analysis_prompt.txt`

Специальный prompt для ChatGPT. Он объясняет, что это рабочий Telegram-чат, какой формат анализа нужен, как обрабатывать задачи, договорённости, риски, открытые вопросы, чувствительные темы, голосовые и вложения.

### `04_image_contact_sheet_*.jpg`

Крупные contact sheets для изображений и скриншотов.

Это не миниатюры “для красоты”, а OCR-friendly визуальные листы. Они помогают быстро просмотреть скриншоты, фото документов, ошибки, таблицы, счета, переписки и интерфейсы.

По умолчанию бот размещает небольшое количество изображений на одном листе, чтобы текст оставался читаемым.

### `05_full_archive.zip`

Полный архив с оригинальными вложениями.

Если на contact sheet текст слишком мелкий или нужно проверить исходный документ, оригинальный файл можно найти в ZIP по `attachments_index.md`.

Если для voice/audio создан transcript cache, в ZIP также добавляются `.transcript.txt` файлы в папку `transcripts/`.

### `transcripts/*.transcript.txt`

Автоматические расшифровки voice/audio, созданные при сборке рабочего пакета, если включена настройка `TRANSCRIBE_VOICE=true`.

Transcript также добавляется в `01_chat_export.md` рядом с соответствующим voice/audio сообщением и в `02_attachments_index.md`. Каждый transcript помечается как automatic transcript с предупреждением: `Automatic transcript. Verify if critical.`

## Почему не просто ZIP

Просто ZIP-архив плохо подходит для AI-анализа:

- модель может не распаковать архив;
- может потеряться связь между файлом и сообщением;
- скриншоты могут быть пропущены;
- голосовые могут остаться без контекста;
- сложно понять, какие файлы действительно важны.

Поэтому бот создаёт не просто архив, а структурированный ChatGPT-ready пакет.

## Технологии

Проект использует:

- Python 3;
- python-telegram-bot;
- SQLite;
- Railway;
- Railway Volumes;
- GitHub-based deployment;
- Pillow для генерации contact sheets;
- OpenAI API / speech-to-text для voice/audio transcription;
- Markdown export pipeline;
- ZIP archive pipeline;
- private control flow через Telegram inline-кнопки;
- quiet group mode;
- ChatGPT-oriented prompt design.

## Хранение данных

Данные хранятся на сервере, где запущен бот.

Используется:

- SQLite-база для сообщений и служебной информации;
- файловое хранилище для вложений;
- `data/transcripts` для cache автоматических расшифровок voice/audio;
- Railway Volume для постоянного хранения между redeploy.

AI-анализ выполняется вручную: пользователь сам загружает подготовленный пакет в ChatGPT. При `TRANSCRIBE_VOICE=true` voice/audio за выбранный период отправляются в OpenAI transcription API во время сборки пакета, а результат сохраняется в transcript cache.

## Безопасность

Основные принципы:

- бот работает только с подтверждёнными чатами;
- новые чаты требуют approval;
- управление доступно только администраторам;
- рабочие пакеты отправляются в личный чат администратора;
- в группах бот работает в quiet mode;
- Telegram bot token хранится только в переменных окружения;
- `OPENAI_API_KEY` хранится только в Railway Variables;
- токены и секреты не должны попадать в GitHub;
- при включённой транскрибации voice/audio отправляются во внешний OpenAI API;
- для чувствительных медицинских, страховых, финансовых и юридических данных нужно учитывать внутренние правила компании;
- есть команды очистки и диагностики хранилища.

## Quiet mode

В рабочих группах бот не должен мешать коллегам. Поэтому основной интерфейс управления вынесен в личный чат с ботом.

В группе бот сохраняет сообщения и файлы, отвечает только на минимальные служебные команды, не отправляет рабочие пакеты и не спамит архивами.

В личке администратор использует:

```text
/report_chats
```

и выбирает нужный чат кнопками.

## Medical assistance context

Бот изначально разрабатывался для рабочих чатов компании medical assistance.

В таких чатах могут встречаться чувствительные профессиональные темы:

- госпитализация;
- травмы;
- ДТП;
- операции;
- медицинская эвакуация;
- репатриация;
- смерть пациента;
- тело / труп;
- полиция;
- страховые споры;
- финансовые претензии;
- счета клиник.

Prompt явно объясняет ChatGPT, что такие темы являются частью профессионального контекста. Их нужно анализировать нейтрально, деловым языком, без сенсационности, без домыслов и без лишних графичных деталей.

## Голосовые сообщения

Бот может автоматически транскрибировать Telegram voice/audio через OpenAI API, но только при сборке рабочего пакета.

При получении сообщения бот не отправляет аудио в OpenAI. Он только сохраняет voice/audio как вложение. Если `TRANSCRIBE_VOICE=true`, то во время сборки пакета бот находит voice/audio за выбранный период, отправляет их в OpenAI transcription API и сохраняет результат в cache:

```text
data/transcripts/YYYY-MM-DD/
```

Для каждого voice/audio создаются:

```text
<chat_id>_<message_id>_<file_unique_id>.transcript.json
<chat_id>_<message_id>_<file_unique_id>.transcript.txt
```

Повторная сборка пакета использует cache и не вызывает OpenAI API заново, если transcript уже есть или если ранее был сохранён `failed/skipped` статус.

Transcript добавляется:

- в `01_chat_export.md` рядом с voice/audio сообщением;
- в `02_attachments_index.md`;
- в ZIP как `.transcript.txt` в папке `transcripts/`.

Если транскрибация недоступна, API вернул ошибку, файл отсутствует или превышает лимит, пакет всё равно собирается. В таком случае в пакет добавляется статус `Transcript unavailable / failed` или `skipped` с причиной.

Автоматические transcript нужно проверять по оригинальному аудио, если деталь критична. В пакет добавляется пометка: `Automatic transcript. Verify if critical.`

Если transcript недоступен, можно использовать ручную расшифровку, например из Telegram Premium.

## Основные команды

### В личном чате с ботом

`/report_chats` — показать подтверждённые чаты и собрать рабочий пакет кнопками.

`/version` — показать текущую версию и сборку.

`/health` — краткий технический статус.

`/storage_status` — показать размер базы, файлов и экспортов.

`/vacuum_db` — сжать SQLite-базу после массовой очистки.

`/cleanup_now` — принудительно запустить retention-очистку.

`/purge_chat <chat_id>` — удалить архив конкретного чата.

`/purge_all_data CONFIRM` — удалить все сохранённые сообщения, файлы и экспорты.

### В группах

`/status` — статус текущего чата.

`/help` — краткая справка.

Рабочие пакеты рекомендуется собирать через личный чат с ботом.

## Переменные окружения

Обязательные:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
ADMIN_USER_IDS=123456789
```

Рекомендуемые:

```env
MAX_FILE_SIZE_MB=50
MAX_TELEGRAM_SEND_SIZE_MB=49
TELEGRAM_SEND_TIMEOUT_SECONDS=180
APP_TIMEZONE=Asia/Bangkok
REPORT_TIMEZONE=Asia/Bangkok
WORK_SHIFT_START_HOUR=9
BOT_VERSION=0.6.0
BOT_BUILD_NAME=openai-voice-transcription
BOT_BUILD_DATE=2026-06-16
WORK_ANALYSIS_PROMPT_PATH=data/config/work_analysis_prompt.txt
```

Contact sheets:

```env
CONTACT_SHEET_IMAGES_PER_PAGE=4
CONTACT_SHEET_PAGE_WIDTH=3000
CONTACT_SHEET_THUMB_WIDTH=1350
CONTACT_SHEET_JPEG_QUALITY=92
```

Railway/Railpack font package for Cyrillic captions in image contact sheets:

```env
RAILPACK_DEPLOY_APT_PACKAGES=fonts-dejavu-core
```

OpenAI voice/audio transcription:

```env
OPENAI_API_KEY=your_openai_api_key
TRANSCRIBE_VOICE=false
TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
TRANSCRIBE_MAX_AUDIO_MB=25
TRANSCRIBE_TIMEOUT_SECONDS=120
```

AI PDF reports:

```env
OPENAI_REPORTS_ENABLED=false
OPENAI_REPORT_MODEL=gpt-5-mini
OPENAI_REPORT_TIMEOUT_SECONDS=180
OPENAI_REPORT_MAX_INPUT_CHARS=250000
OPENAI_REPORT_TEMPERATURE=0.2
OPENAI_REPORT_INCLUDE_IMAGES=true
OPENAI_REPORT_MAX_IMAGES=6
OPENAI_REPORT_TWO_PASS=true
AI_REPORT_LOGO_PATH=assets/bs_logo.png
```

## AI PDF Reports

The bot can generate a compact short PDF report in Russian through the OpenAI API.

This feature is disabled by default. To enable it:

```env
OPENAI_REPORTS_ENABLED=true
```

When enabled, the private admin `/report_chats` flow shows an extra `📄 Краткий PDF` option for the selected chat/period. The bot builds text inputs from `chat_export.md`, `attachments_index.md`, and the current work analysis prompt, adds image contact sheets when `OPENAI_REPORT_INCLUDE_IMAGES=true`, asks OpenAI for a compact 1–2 page report, renders it as PDF, and sends it to the admin in private chat.

The report menu also includes “с прошлого понедельника”: this period starts at 09:00 on Monday of the previous calendar week and ends at the current moment in `REPORT_TIMEZONE`.

The AI PDF report is still an MVP. It aims to keep the report concise without dropping important decisions, amounts, risks, and action items. The tasks section is rendered as a real PDF table with owner, deadline, and status columns when the model returns structured data.

Image contact sheets help the model read visible text in screenshots, such as prices, currencies, access errors, and partner messages. `OPENAI_REPORT_MAX_IMAGES` limits how many contact sheet pages are sent. Including images may slightly increase OpenAI API cost.

`AI_REPORT_LOGO_PATH` can point to a PNG/JPEG logo used in the PDF header. If the file is missing or unreadable, the PDF is generated without a logo. The repository keeps the source SVG at `assets/bs_logo.svg`.

The report uses the same custom prompt system as work packages. To update the private style guide/prompt on Railway, send a `.txt` file to the bot with caption `/set_work_prompt`, then check `/prompt_status`.

If the feature is disabled or `OPENAI_API_KEY` is missing, the bot shows a clear error and does not affect the existing work package flow.

## Custom Work Analysis Prompt

The work package includes `work_analysis_prompt.txt`. Its template can be customized without editing `bot.py`.

The public generic example is:

```text
prompts/work_analysis_prompt.example.txt
```

For a private deployment, copy it to:

```text
data/config/work_analysis_prompt.txt
```

or set:

```env
WORK_ANALYSIS_PROMPT_PATH=/custom/path/work_analysis_prompt.txt
```

The template supports placeholders:

```text
{chat_title}
{period_label}
```

Private prompts should not be committed. On Railway, keep the private prompt in the persistent volume:

```text
/app/data/config/work_analysis_prompt.txt
```

Railway upload flow:

1. Open a private chat with the bot as an admin.
2. Send the private `.txt` prompt file with caption `/set_work_prompt`.
3. Check `/prompt_status`.

The bot stores the prompt in the Railway volume and creates a backup before replacing an existing custom prompt. To disable the custom prompt and return to the public example/fallback, use `/reset_work_prompt`.

If the private file is missing, the bot uses `prompts/work_analysis_prompt.example.txt`. If that is also missing, it falls back to a short built-in generic prompt.

## Deployment

Пошаговая инструкция по развёртыванию находится в файле:

```text
DEPLOYMENT_RU.md
```

## Current version

```text
Version: 0.6.0
Build: openai-voice-transcription
Build date: 2026-06-16
```

Enabled features:

```text
- approved chats
- button-first private admin interface
- private /report_chats with buttons
- work packages for 7 days
- work shift packages 09:00–now
- attachments index
- OCR-friendly image contact sheets
- OpenAI voice/audio transcription
- transcript cache
- transcript files in ZIP
- full archive ZIP
- cleanup / purge commands
- storage diagnostics
- quiet group mode
```
