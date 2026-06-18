# Инструкция по развёртыванию Telegram Work Summary Bot

Эта инструкция описывает полный путь от создания Telegram-бота до запуска на Railway.

## 1. Что понадобится

Нужно заранее иметь:

- аккаунт Telegram;
- аккаунт GitHub;
- аккаунт Railway;
- доступ к репозиторию с кодом бота;
- понимание, какие Telegram-чаты бот должен архивировать.

## 2. Создать Telegram-бота через BotFather

1. Открой Telegram.
2. Найди официального бота `@BotFather`.
3. Отправь команду `/newbot`.
4. Задай имя бота, например `Work Summary Bot`.
5. Задай username бота. Он должен заканчиваться на `bot`, например `my_work_summary_bot`.
6. BotFather выдаст токен вида `1234567890:AA...`.

Этот токен нужно сохранить. Это секретный ключ бота.

Нельзя публиковать токен в GitHub, отправлять в общий чат или вставлять в README.

## 3. Получить свой Telegram user ID

Боту нужно знать, кто является администратором.

Самый простой способ:

1. Написать любому Telegram-боту для получения user ID, например `@userinfobot`.
2. Скопировать свой numeric user ID.
3. Позже добавить его в Railway variable `ADMIN_USER_IDS`.

Если администраторов несколько, IDs указываются через запятую:

```env
ADMIN_USER_IDS=123456789,987654321
```

## 4. Подготовить GitHub-репозиторий

Есть два варианта.

### Вариант A: fork

1. Открыть исходный репозиторий на GitHub.
2. Нажать `Fork`.
3. Создать копию репозитория в своём аккаунте.
4. В Railway подключать уже свой fork.

### Вариант B: clone / duplicate

1. Скачать или клонировать репозиторий.
2. Создать новый пустой репозиторий в GitHub.
3. Залить туда код.
4. Подключить этот репозиторий к Railway.

## 5. Проверить структуру проекта

В репозитории должны быть как минимум:

```text
bot.py
requirements.txt
Procfile
runtime.txt
README.md
DEPLOYMENT_RU.md
```

`Procfile` должен запускать worker:

```text
worker: python bot.py
```

`requirements.txt` должен включать зависимости проекта, включая `python-telegram-bot`, `python-dotenv` и `Pillow`.
Для автоматической транскрибации voice/audio также используется `openai`.

## 6. Создать проект на Railway

1. Открыть Railway.
2. Создать новый project.
3. Выбрать deploy from GitHub repo.
4. Подключить репозиторий с ботом.
5. Выбрать сервис worker.
6. Дождаться первого build/deploy.

Если Railway попросит команду запуска, использовать:

```text
python bot.py
```

Но обычно Railway прочитает `Procfile`.

## 7. Добавить переменные окружения

В Railway открыть сервис и перейти в `Variables`.

Добавить обязательные переменные:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
ADMIN_USER_IDS=your_telegram_user_id
```

Рекомендуемые переменные:

```env
MAX_FILE_SIZE_MB=50
MAX_TELEGRAM_SEND_SIZE_MB=49
TELEGRAM_SEND_TIMEOUT_SECONDS=180
APP_TIMEZONE=Asia/Bangkok
WORK_SHIFT_START_HOUR=9
BOT_VERSION=0.6.0
BOT_BUILD_NAME=openai-voice-transcription
BOT_BUILD_DATE=2026-06-16
```

Contact sheet settings:

```env
CONTACT_SHEET_IMAGES_PER_PAGE=4
CONTACT_SHEET_PAGE_WIDTH=3000
CONTACT_SHEET_THUMB_WIDTH=1350
CONTACT_SHEET_JPEG_QUALITY=92
```

Railway/Railpack package для корректной кириллицы в image contact sheets:

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

После изменения variables Railway обычно делает redeploy.

## 7.1 OpenAI API для транскрибации голосовых

ChatGPT Plus и OpenAI API — это разные продукты. Подписка ChatGPT Plus не даёт автоматически API-доступ и не оплачивает API-запросы.

Для транскрибации voice/audio нужно:

1. Создать отдельный OpenAI Project.
2. Включить billing или добавить credits.
3. Создать API key внутри этого проекта.
4. Не публиковать ключ и не коммитить его в GitHub.
5. Добавить ключ в Railway Variables как `OPENAI_API_KEY`.

Рекомендуемый безопасный rollout:

1. Сначала оставить `TRANSCRIBE_VOICE=false`.
2. Сделать deploy.
3. Проверить `/version` и `/health`.
4. Собрать обычный рабочий пакет без транскрибации.
5. Потом включить `TRANSCRIBE_VOICE=true`.
6. Протестировать на одном коротком voice в тестовом чате.

Транскрибация выполняется только при сборке рабочего пакета. При получении сообщений бот сохраняет voice/audio как обычные вложения и не отправляет их в OpenAI API.

## 8. Добавить Railway Volume

Volume нужен, чтобы база и файлы не пропадали после redeploy.

1. Открыть проект Railway.
2. Добавить Volume к worker-сервису.
3. Указать mount path, соответствующий папке данных бота.

В текущей версии бот использует папку:

```text
/app/data
```

Если volume монтируется в другой путь, нужно убедиться, что код и Railway mount path согласованы.

После подключения volume перезапустить deployment.

## 9. Первый запуск

Открыть личный чат с ботом в Telegram и отправить:

```text
/start
```

Потом проверить версию:

```text
/version
```

Проверить технический статус:

```text
/health
```

Если всё хорошо, бот покажет, что база и папки хранения доступны.

## 10. Добавить бота в Telegram-чат

1. Открыть нужный групповой чат.
2. Добавить туда бота.
3. Написать любое сообщение или команду.
4. Бот должен сообщить, что чат ожидает подтверждения администратора.
5. Администратор подтверждает чат кнопкой или командой.

После подтверждения бот начинает сохранять сообщения и файлы.

## 11. Основной рабочий сценарий

Рабочий чат лучше не засорять командами.

Основное управление выполняется в личном чате с ботом:

```text
/start
```

Бот покажет личный кабинет администратора с кнопками. Старые текстовые команды, например `/report_chats`, остаются fallback и продолжают работать, если написать их вручную.

Для каждого чата доступны варианты:

- `Смена 09:00–сейчас`;
- `7 дней`.

После выбора бот пришлёт пакет:

```text
01_chat_export.md
02_attachments_index.md
03_work_analysis_prompt.txt
04_image_contact_sheet_*.jpg
05_full_archive.zip
```

Если включена `TRANSCRIBE_VOICE=true`, голосовые и audio за выбранный период будут расшифрованы при сборке пакета. Transcript попадёт в `01_chat_export.md`, `02_attachments_index.md` и в ZIP в папку `transcripts/`.

## 12. Как анализировать пакет в ChatGPT

Загрузить в ChatGPT:

1. `01_chat_export.md`;
2. `02_attachments_index.md`;
3. `03_work_analysis_prompt.txt`;
4. contact sheets;
5. при необходимости `05_full_archive.zip`.

Затем написать:

```text
Выполни анализ по инструкции из work_analysis_prompt.txt.
```

Если в пакете есть автоматические transcript, они помечаются предупреждением `Automatic transcript. Verify if critical.` Критичные медицинские, финансовые или юридические детали лучше сверять с оригинальным аудио.

Если transcript недоступен, ChatGPT может попросить вручную прислать расшифровку. В этом случае можно использовать ручную расшифровку, например из Telegram Premium.

## 13. Проверка хранения

Команды для диагностики:

```text
/storage_status
```

Показывает реальный размер базы, файлов, экспортов и data directory.

```text
/health
```

Показывает краткий статус.

```text
/vacuum_db
```

Сжимает SQLite-базу после удаления большого количества сообщений.

## 14. Очистка данных

Принудительная retention-очистка:

```text
/cleanup_now
```

Удаление архива конкретного чата:

```text
/purge_chat <chat_id>
```

Полная очистка всех сообщений, файлов и экспортов:

```text
/purge_all_data CONFIRM
```

Список известных чатов при этом сохраняется.

## 15. Безопасность

Основные принципы:

- токен Telegram хранится только в Railway Variables;
- токен не должен попадать в GitHub;
- бот сохраняет данные только из подтверждённых чатов;
- управление доступно только ADMIN_USER_IDS;
- рабочие пакеты отправляются в личный чат администратора;
- в группах бот работает в quiet mode;
- AI-анализ выполняется вручную через ChatGPT после загрузки пакета пользователем;
- `OPENAI_API_KEY` хранится только в Railway Variables;
- ключ OpenAI нельзя публиковать, пересылать в общие чаты или коммитить в GitHub;
- при `TRANSCRIBE_VOICE=true` voice/audio отправляются во внешний OpenAI API для транскрибации;
- не включать транскрибацию без согласования, если внутренние правила компании запрещают такую обработку;
- критичные медицинские, финансовые и юридические детали нужно проверять по оригинальному аудио.

## 16. Эксплуатационные рекомендации

- Не добавлять бота в лишние чаты.
- Не давать доступ к Railway посторонним.
- Не публиковать Railway variables.
- Периодически проверять `/storage_status`.
- Перед крупными изменениями делать git commit.
- После deploy проверять `/version`.
- В рабочих чатах использовать quiet mode.
- Для отчётов пользоваться `/start` в личке.
- Если включена транскрибация, периодически проверять `data/transcripts` и Railway logs.

## 16.1 Troubleshooting транскрибации

Если transcript не появился:

- проверить `TRANSCRIBE_VOICE`;
- проверить `OPENAI_API_KEY`;
- проверить billing или credits в OpenAI Project;
- посмотреть Railway logs.

Если OpenAI API вернул ошибку, рабочий пакет всё равно должен собраться. Причину можно искать в Railway logs и в failed cache внутри `data/transcripts/YYYY-MM-DD/`.

Если повторная сборка не вызывает API, это ожидаемо: бот использует transcript cache. Cache со статусами `ok`, `failed` и `skipped` предотвращает повторные попытки.

Если audio format не поддержан, временно выключить:

```env
TRANSCRIBE_VOICE=false
```

и чинить поддержку формата отдельным patch.

Если API отвечает слишком долго, проверить:

```env
TRANSCRIBE_TIMEOUT_SECONDS=120
```

## 17. Возможные улучшения

Планируемые функции:

- автоматическая отправка пакета администратору в 09:00;
- отдельный daily report mode для selected chats;
- улучшение качества и диагностики автоматической транскрибации voice/audio;
- более умная классификация скриншотов;
- PDF-отчёт прямо из бота;
- интеграция с OpenAI API для полностью автоматической сводки.
