# Запуск на Railway

Проект работает как постоянно запущенный фоновый процесс. Код уже содержит Dockerfile и поддержку авторизации через переменную `TELEGRAM_SESSION`. Авторизация по номеру и коду выполняется один раз на твоём компьютере.

## 1. Подготовь Telegram локально

Скачай репозиторий, установи Python 3.12+, открой терминал в папке проекта:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

На Windows: `.venv\Scripts\activate`; скопируй `.env.example` в `.env` средствами проводника.

В локальном `.env` заполни `API_ID` и `API_HASH` из [my.telegram.org](https://my.telegram.org). Оставь `DATA_DIR=data` и пустой `TELEGRAM_SESSION`. Выполни:

```bash
python -m catalog_bot login
python -m catalog_bot session-env
```

Введи номер, код Telegram и пароль 2FA, если он есть, в локальном терминале. Первая команда покажет Telegram ID аккаунта: это `OWNER_ID`, если управляешь с него же. Вторая сохранит `data/railway-session.env` с готовой переменной. Открой файл локально и перенеси его содержимое прямо в Railway Variables. Эта строка даёт доступ к аккаунту: не отправляй её в чат, GitHub или логи. Файл исключён из Git и Docker.

Создай отдельного управляющего бота через [@BotFather](https://t.me/BotFather), команда `/newbot`; его токен понадобится в `BOT_TOKEN`.

## 2. Создай сервис

1. В Railway создай проект и сервис из GitHub-репозитория **Danya1221/45758535**, ветка **main**. Корень исходников — корень репозитория.
2. Подключи к этому сервису постоянный **Volume**, путь монтирования **`/data`**. В нём будут SQLite, настройки, прайс, история изменений и журнал отправок. Приложение проверяет, что его данные находятся внутри Volume.
3. В **Variables** через **RAW Editor** добавь настройки из таблицы ниже и `TELEGRAM_SESSION` из локального файла.
4. Оставь один экземпляр сервиса (**1 replica**), выключи **Serverless / App Sleeping**. Это приложение постоянно слушает Telegram.
5. Railway использует корневой `Dockerfile`. Команда уже задана в нём: `python -m catalog_bot run`. Build Command и Pre-deploy Command оставь пустыми; логин не выполняется во время сборки.
6. **Healthcheck Path** оставь пустым: приложение не запускает HTTP-сервер. Публичный домен для бота не нужен.
7. Примени изменения и запусти Deploy. В логах не должно быть ошибок авторизации или Volume. Реальный приём прайса проверь через `/status` и `/changes` в управляющем боте.

| Переменная | Значение |
| --- | --- |
| `API_ID` | Число из my.telegram.org |
| `API_HASH` | Строка из my.telegram.org |
| `BOT_TOKEN` | Токен нового бота от BotFather |
| `OWNER_ID` | Telegram ID владельца управления |
| `TELEGRAM_SESSION` | Значение из `data/railway-session.env` |
| `DATA_DIR` | `/data` |
| `PRICE_BOT_USERNAME` | `@tech_online_seller_bot` |
| `PRICE_REQUEST_TEXT` | `/prices` |
| `PRICE_REFRESH_SECONDS` | `600` |
| `MAX_REPLIES_PER_MINUTE` | `10` |
| `DEDUP_SECONDS` | `120` |

`PRICE_BUTTON_TEXT` оставь пустым. `CONTACT` можно заполнить контактом для текста ответа. `RAILWAY_VOLUME_MOUNT_PATH` Railway выставляет сам после подключения Volume; вручную задавать его не нужно. Если `DATA_DIR` не задан, приложение использует этот путь автоматически. Локальное `DATA_DIR=data` при переносе настроек замени на `/data`.

## 3. Включи ответы

Открой личку с новым управляющим ботом и нажми Start. Проверь `/status`: должен загрузиться полный актуальный прайс. Через `/chats` выбери нужные группы, через `/test` проверь несколько настоящих запросов. Затем отправь `/run`.

Ответы отправляются от авторизованного пользовательского аккаунта в выбранные группы на исходные сообщения. При первом запуске они выключены. Прайс обновляется каждые 600 секунд после завершения предыдущего обновления; при сбое получения ответы приостанавливаются до свежего полного прайса. Результат обновления и изменения цен доступны через `/changes`.

Не запускай одновременно локальный `run` и Railway с теми же ботом и пользовательским аккаунтом. Сохраняй Volume при обновлениях сервиса. После перезапуска выбранные чаты, прайс и режим работы восстанавливаются из базы; с автоисточником сначала запрашивается свежий прайс.

Код проверен локально без сети Telegram. Запуск в Railway и авторизацию проверяют после добавления реальных настроек.

Документация Railway: [Dockerfile](https://docs.railway.com/builds/dockerfiles), [Volumes](https://docs.railway.com/volumes), [Variables](https://docs.railway.com/variables), [Healthchecks](https://docs.railway.com/deployments/healthchecks), [Serverless](https://docs.railway.com/deployments/serverless).
