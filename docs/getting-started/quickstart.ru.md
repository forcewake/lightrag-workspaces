# Быстрый старт

LightRAG Workspaces добавляет выбор отдельной базы в интерфейс и API. Каждая база хранится в своей папке и открывается в отдельном процессе. Используется закреплённый LightRAG 1.5.7.

## Новая установка

Установите Docker с Compose и подготовьте доступ к LLM и embedding-провайдерам.

```sh
git clone https://github.com/forcewake/lightrag-workspaces.git
cd lightrag-workspaces
cp .env.example .env
chmod 600 .env
```

Заполните `.env`: адреса, ключи, модели, `EMBEDDING_DIM`. Если переносите готовый индекс, сохраните исходную embedding-модель и размерность. Совпадения одной размерности недостаточно.

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python lightrag /opt/lightrag-multiworkspace/scripts/init_workspace.py default
docker compose up -d
```

Откройте [localhost:9621/webui/](http://localhost:9621/webui/). По умолчанию порт доступен только с Docker-хоста. Для доступа с другой машины настройте защищённый вход или SSH-туннель.

## Добавить базу

```sh
docker compose run --rm --no-deps --entrypoint python lightrag /opt/lightrag-multiworkspace/scripts/init_workspace.py research
```

Обновите страницу, выберите `research`, загрузите файл через **Documents** и дождитесь завершения обработки. Затем перейдите в **Retrieval**. Диалоги и настройки в браузере сохраняются отдельно для каждой базы.

## API

```sh
curl http://localhost:9621/workspaces

curl http://localhost:9621/workspaces/research/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"Что сказано в документах?","mode":"mix"}'
```

Альтернатива: обычный `/query` с заголовком `LIGHTRAG-WORKSPACE: research`. Без выбора используется `LIGHTRAG_DEFAULT_WORKSPACE`. Swagger выбранной базы — `/workspaces/research/docs`. При включённой аутентификации передавайте ключ или bearer-токен. [Все примеры API](../guides/api.md).

## Изменить базу по умолчанию { #change-default }

Выберите существующий точный идентификатор базы. Перед изменением default закрепите прежний default в `LIGHTRAG_LEGACY_STORAGE_WORKSPACE` и больше не меняйте это значение:

```dotenv
LIGHTRAG_LEGACY_STORAGE_WORKSPACE=default
LIGHTRAG_DEFAULT_WORKSPACE=meeting-notes
```

Пересоздайте контейнер и откройте `/webui/`. Интерфейс и API без выбора используют `meeting-notes`. Явные URL и заголовки `LIGHTRAG-WORKSPACE` сохраняют свой выбор; открытые вкладки с таким URL остаются в прежней базе.

История и настройки браузера остаются привязаны к своим базам: `default` сохраняет прежние общие ключи, а `meeting-notes` — свои отдельные ключи. Серверные документы не перемещаются и не переименовываются. Если `LIGHTRAG_LEGACY_STORAGE_WORKSPACE` не задан, он совпадает с default для совместимости со старыми установками. [Полная конфигурация](../reference/configuration.md#change-the-default-workspace).

## Существующие данные

Остановите старый сервис, сделайте проверенную резервную копию и сначала испытайте расширение на копии данных. Не переименовывайте папки, не меняйте модели и не запускайте двух писателей на одном индексе.

Если осталась незавершённая очередь импорта, база откроется для совместимого поиска, но изменения вернут `409`. Расширение не возобновляет старую обработку и не мигрирует данные автоматически.

[Перенос данных](../guides/existing-data.md) · [Unraid](../guides/unraid.md) · [Откат](../guides/backup-and-rollback.md)

## Что важно помнить

- Workspace разделяет данные и процессы, но не права разных пользователей.
- Ключи и модели общие для всех баз.
- Поддерживается только файловый набор хранилищ JsonKV/JsonDocStatus/NanoVectorDB/NetworkX.
- Большая база может долго открываться. `503` может означать занятые процессы, нехватку памяти или ошибку запуска. Проверьте лог выбранного workspace; повторяющиеся тайм-ауты блокировки PostgreSQL при запуске графа исправлены в 0.2.3.
- Пример ограничивает контейнер 8 GiB; размер лимита нужно выбрать под ваши данные. Не увеличивайте число процессов без проверки памяти и лимитов провайдеров.

[Настройки](../reference/configuration.md) · [Архитектура и ограничения](../reference/architecture.md) · [Поиск проблем](../reference/troubleshooting.md)
