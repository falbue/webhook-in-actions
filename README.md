# Deploy API (FastAPI + SQLModel)

Полный рефактор сервиса деплоя на FastAPI с SQLModel.

## Возможности

- Авторизация по бессрочному API-ключу (`X-API-Key`)
- Роли пользователей: `basic`, `premium`, `admin`
- Лимиты:
  - `basic`: максимум 1 деплой
  - `premium` и `admin`: без лимита
- Деплой приложений через Docker Compose из GHCR
- Управление ENV только для своих деплоев (`GET/PUT/PATCH`)
- Создание PostgreSQL-инстансов через API
- Порты на пользователя выделяются блоком `x000-x999`
- Внутри блока пользователя:
  - приложение: `x000-x899`
  - база: `x900-x999`

## Стек

- FastAPI
- SQLModel (SQLAlchemy)
- Uvicorn
- Docker CLI + Docker Compose plugin внутри контейнера

## Модель безопасности

- Все endpoint'ы (кроме `/health`) требуют заголовок `X-API-Key`
- Ключ хранится в БД только в SHA-256 хеше
- Только админ может:
  - создавать пользователей
  - менять роли
  - сбрасывать ключи

## Пример docker-compose.yml
```yaml
services:
  deploy:
    image: ghcr.io/falbue/deploy:{tag}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /opt/deploy:/data:rw
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - DEPLOY_ROOT=${DEPLOY_ROOT}
      - DB_ROOT=${DB_ROOT}
      - DB_NET_NAME=${DB_NET_NAME}
      - WEB_NET_NAME=${WEB_NET_NAME}
      - ENABLE_NGINX_GATEWAY=${ENABLE_NGINX_GATEWAY:-true}
      - NGINX_GATEWAY_ROOT=${NGINX_GATEWAY_ROOT}
      - DOCKER_AUTH_ROOT=${DOCKER_AUTH_ROOT}
      - USER_PORT_BLOCK_START=${USER_PORT_BLOCK_START}
      - APP_PORT_OFFSET_START=${APP_PORT_OFFSET_START}
      - APP_PORT_OFFSET_END=${APP_PORT_OFFSET_END}
      - DB_PORT_OFFSET_START=${DB_PORT_OFFSET_START}
      - DB_PORT_OFFSET_END=${DB_PORT_OFFSET_END}
      - INIT_ADMIN_API_KEY=${INIT_ADMIN_API_KEY}
      - INIT_ADMIN_USERNAME=${INIT_ADMIN_USERNAME}
    restart: unless-stopped
    ports:
      - "127.0.0.1:1500:8080"
```

## Инициализация БД

Таблицы создаются автоматически на старте приложения (`SQLModel.metadata.create_all`).

Если задан `INIT_ADMIN_API_KEY`, при старте создается администратор:

- `INIT_ADMIN_USERNAME` (по умолчанию `admin`)
- `INIT_ADMIN_API_KEY` (обязательно для автосоздания)

## Переменные окружения

См. `.env-template`:

- `DATABASE_URL=sqlite:////data/deploy.db`
- `DEPLOY_ROOT=/data/deployments`
- `DB_ROOT=/data/databases`
- `DB_NET_NAME=db-net`
- `WEB_NET_NAME=web-net`
- `ENABLE_NGINX_GATEWAY=true`
- `NGINX_GATEWAY_ROOT=/data/nginx-gateway`
- `DOCKER_AUTH_ROOT=/data/docker-auth`
- `USER_PORT_BLOCK_START=2`
- `APP_PORT_OFFSET_START=0`
- `APP_PORT_OFFSET_END=899`
- `DB_PORT_OFFSET_START=900`
- `DB_PORT_OFFSET_END=999`
- `INIT_ADMIN_USERNAME=admin`
- `INIT_ADMIN_API_KEY=...`

## Логика портов на пользователя

Для каждого пользователя определяется персональный диапазон:

- `x = USER_PORT_BLOCK_START + (user_id - 1)`
- общий диапазон пользователя: `x000-x999`
- app: `x000-x899`
- db: `x900-x999`

Пример при `USER_PORT_BLOCK_START=2`:

- `user_id=1` -> `2000-2999` (app: `2000-2899`, db: `2900-2999`)
- `user_id=2` -> `3000-3999` (app: `3000-3899`, db: `3900-3999`)

## Запуск

1. Создайте env файл

```bash
cp .env-template .env
# обязательно заполните INIT_ADMIN_API_KEY
```

2. Поднимите сервис

```bash
docker compose up -d
```

3. Проверка

```bash
curl http://127.0.0.1:1500/health
```

## Основные endpoint'ы

### Системные

- `GET /health`
- `GET /me`
- `POST /ghcr/login`
- `POST /ghcr/logout`

### Админ

- `POST /admin/users`
- `GET /admin/users`
- `PATCH /admin/users/{user_id}/role`
- `POST /admin/users/{user_id}/reset-api-key`

### Деплои

- `POST /deployments`
- `GET /deployments`
- `POST /deployments/{owner/repo}/redeploy`
- `POST /deployments/{owner/repo}/apply`
- `DELETE /deployments/{owner/repo}`

### ENV

- `GET /deployments/{owner/repo}/env`
- `PUT /deployments/{owner/repo}/env` (полная замена)
- `PATCH /deployments/{owner/repo}/env` (частичное обновление)

### Базы данных

- `POST /databases`
- `GET /databases`
- `POST /databases/{database_id}/apply`
- `DELETE /databases/{database_id}`

### Nginx и SSL

- `POST /deployments/{owner/repo}/nginx/preview/preset-api`
- `POST /deployments/{owner/repo}/nginx/preset-api`
- `PUT /deployments/{owner/repo}/nginx/custom`
- `POST /deployments/{owner/repo}/nginx/certbot`
- `DELETE /deployments/{owner/repo}/nginx?domain=example.com`

## Примеры запросов

Создать пользователя (admin key):

```bash
curl -X POST http://127.0.0.1:1500/admin/users \
  -H "X-API-Key: ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username":"user1","role":"basic"}'
```

Создать деплой:

```bash
curl -X POST http://127.0.0.1:1500/deployments \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"owner_repo":"falbue/swipe-refactor","tag":"latest","run_deploy":true}'
```

Обновить ENV частично:

```bash
curl -X PATCH http://127.0.0.1:1500/deployments/falbue/swipe-refactor/env \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"values":{"APP_ENV":"prod","TOKEN":"secret"}}'
```

Создать БД:

```bash
curl -X POST http://127.0.0.1:1500/databases \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"main","deployment_repo":"falbue/swipe-refactor","postgres_image":"postgres:18","postgres_user":"db_user","postgres_password":"db_pass","postgres_db":"db_name","run_deploy":true}'
```

Запустить БД, созданную ранее с `run_deploy=false`:

```bash
curl -X POST http://127.0.0.1:1500/databases/1/apply \
  -H "X-API-Key: USER_KEY"
```

Логин в приватный GHCR:

```bash
curl -X POST http://127.0.0.1:1500/ghcr/login \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"github_username":"your-github-login","github_token":"ghp_xxx"}'
```

Логаут из GHCR:

```bash
curl -X POST http://127.0.0.1:1500/ghcr/logout \
  -H "X-API-Key: USER_KEY"
```

Удалить БД:

```bash
curl -X DELETE http://127.0.0.1:1500/databases/1 \
  -H "X-API-Key: USER_KEY"
```

Удалить деплой:

```bash
curl -X DELETE http://127.0.0.1:1500/deployments/falbue/swipe-refactor \
  -H "X-API-Key: USER_KEY"
```

Создать nginx preset для API:

```bash
curl -X POST http://127.0.0.1:1500/deployments/falbue/swipe-refactor/nginx/preset-api \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain":"api.example.com","force_https":true}'
```

Предпросмотр preset-конфига без сохранения:

```bash
curl -X POST http://127.0.0.1:1500/deployments/falbue/swipe-refactor/nginx/preview/preset-api \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain":"api.example.com","force_https":true,"use_ssl":false}'
```

Выпустить SSL сертификат через certbot и включить HTTPS:

```bash
curl -X POST http://127.0.0.1:1500/deployments/falbue/swipe-refactor/nginx/certbot \
  -H "X-API-Key: USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain":"api.example.com","email":"ops@example.com","staging":false}'
```

## Примечание по compose для БД

Сервис генерирует compose с параметрами PostgreSQL из запроса `POST /databases`:

- `postgres_image`
- `postgres_user`
- `postgres_password`
- `postgres_db`
- сеть `db-net` (external)
- порт `127.0.0.1:<выделенный_порт>:5432`

## Nginx Gateway

Сервис управляет встроенным gateway-стеком в `NGINX_GATEWAY_ROOT`, если
`ENABLE_NGINX_GATEWAY=true`:

- `nginx:alpine` (порты `80/443`)
- `certbot/certbot` с автообновлением сертификатов каждые 12 часов
- сеть `web-net` (external)

Все app-контейнеры автоматически подключаются к `web-net`, поэтому nginx может проксировать на любой деплой.

Перед сохранением и применением nginx-конфига API выполняет `nginx -t`; при ошибке конфиг откатывается.

Если `ENABLE_NGINX_GATEWAY=false`, встроенный nginx/certbot не используется,
`/deployments/{owner/repo}/nginx/*` endpoint'ы возвращают `409`, а app-контейнеры не
подключаются к `web-net`.



