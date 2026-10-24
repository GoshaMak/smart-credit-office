# Проверка стека Python/sqlc/PostgreSQL

Этот минимальный spike проверяет выбранный стек из архитектуры: Python 3.14, uv, sqlc с Python WASM-плагином, SQL-файлы и PostgreSQL 16. Он содержит одну таблицу и две сгенерированные операции. Это проверка инструментария, а не основа схемы предметной области.

## Зафиксированные компоненты

- CPython `3.14.4` (версия также закреплена в `.python-version`; ограничение проекта: `>=3.14,<3.15`), uv `0.13.5`.
- sqlc `1.31.1`; запускать ту же версию, что стоит у разработчика/в CI.
- Официальный плагин `sqlc-gen-python` `1.3.0`, закреплён SHA256 в `sqlc.yaml`.
- Python зависимости разрешены uv и зафиксированы в `uv.lock`; integration driver — Psycopg `3.3.6`; генератор использует async SQLAlchemy Core `2.0.54` для `AsyncConnection`. SQL запросы и модели создаёт sqlc, ORM-модели не используются.
- PostgreSQL `16.15`, образ закреплён версией и digest в отдельном Compose проекте. Данные контейнера хранятся в tmpfs; Compose публикует порт `55439` только на localhost.

## Повторный запуск

Из корня репозитория:

```sh
uv sync --project backend --all-groups --locked
uv run --project backend --locked python backend/scripts/check_codegen.py
```

Скрипт проверяет версию sqlc `1.31.1`, точный ожидаемый набор генерируемых файлов, запускает sqlc и сравнивает SHA256 до и после генерации. Он работает с незакоммиченными файлами и не требует `git add`. Сгенерированный код лежит в `backend/src/generated/stack_check`.

Поднять выделенную тестовую БД:

```sh
docker compose -p hackathon-stage0-stackcheck -f backend/compose.stack-check.yaml up --detach --wait
```

Запустить интеграционные проверки на этой БД:

```sh
STACK_CHECK_DATABASE_URL='postgresql+psycopg://stack_check:stack-check-local-only@127.0.0.1:55439/stack_check' \
  uv run --project backend --locked python -m pytest backend/tests/test_stack_check.py
```

Проверки создают тестовую таблицу из минимальной миграции и подтверждают:

1. Выполнение созданных sqlc async методов с PostgreSQL и точный round-trip UUID, NUMERIC→`Decimal`, JSONB→словарь и `timestamptz`→timezone-aware datetime. При записи словаря вызывающий слой передаёт `psycopg.types.json.Jsonb(value)`: сгенерированный JSONB аргумент имеет тип `Any`, а psycopg не адаптирует обычный dict без явного wrapper.
2. Транзакцию, которой управляет вызывающая сторона, и rollback при исключении: запись после rollback отсутствует.

Завершить именно этот временный проект:

```sh
docker compose -p hackathon-stage0-stackcheck -f backend/compose.stack-check.yaml down
```

Именованный volume не создаётся; тестовые данные удаляются вместе с контейнером.

Интеграционные проверки выше были выполнены локально на PostgreSQL `16.15`. Текущий workflow [.github/workflows/contracts.yml](../.github/workflows/contracts.yml) проверяет контракт и его regression cases; PostgreSQL integration job в нём пока нет. Полная CI проверка окружения, генерации и интеграционных тестов относится к этапу 1 плана.

Официальные источники: [sqlc configuration and plugins](https://docs.sqlc.dev/en/stable/reference/config.html), [sqlc-gen-python](https://github.com/sqlc-dev/sqlc-gen-python), [SQLAlchemy PostgreSQL asyncio dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#async-with-psycopg), [Psycopg JSON adaptation](https://www.psycopg.org/psycopg3/docs/basic/adapt.html#json-adaptation).

В sandbox, где домашняя uv cache директория недоступна, перед командами `uv` задавать `UV_CACHE_DIR=/tmp/hackathon-uv-stack-cache`.
