# HLViewer — Розгортання з Docker

---

## 1. Передумови на сервері

```bash
apt update && apt install -y docker.io docker-compose-v2 git
```

Перевірка:

```bash
docker --version        # Docker 24+
docker compose version  # v2.x
git --version
```

---

## 2. Отримати файли проєкту

Клонуємо обидва репозиторії (потрібен інтернет):

```bash
mkdir -p /opt/hlviewer
cd /opt/hlviewer

git clone https://github.com/volodymyr-vereshchak/HLViewer.git  HLViewer
git clone https://github.com/volodymyr-vereshchak/frontend.git   frontend
```

---

## 3. Налаштування .env.v2

```bash
nano /opt/hlviewer/HLViewer/.env.v2
```

Вставити вміст (замінити значення де вказано):

```env
# ── База даних ────────────────────────────────────────────────────────────────
POSTGRES_DB=hostlib_db_v2
POSTGRES_USER=YOUR_DB_USER
POSTGRES_PASSWORD=YOUR_DB_PASSWORD
DB_HOST=localhost
DB_PORT=5434
# DATABASE_URL повинен відповідати POSTGRES_USER / POSTGRES_PASSWORD / DB_HOST / DB_PORT
DATABASE_URL=postgresql+asyncpg://YOUR_DB_USER:YOUR_DB_PASSWORD@localhost:5434/hostlib_db_v2

# ── Автентифікація ────────────────────────────────────────────────────────────
JWT_SECRET=ЗАМІНИТИ_openssl_rand_-hex_32
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ЗАМІНИТИ_ПАРОЛЬ
# Viewer-акаунт — авто-сідується при старті, роль viewer_all (всі філії, без адмін-панелі)
DEFAULT_USERNAME=viewer
DEFAULT_PASSWORD=ЗАМІНИТИ_ПАРОЛЬ
# AUTO_LOGIN=true — автоматичний вхід як DEFAULT_USERNAME без сесії
AUTO_LOGIN=true

# ── CORS ─────────────────────────────────────────────────────────────────────
# Додати IP сервера до списку
CORS_ORIGINS=http://localhost:3001,http://127.0.0.1:3001,http://<IP-СЕРВЕРА>:3001

# ── Звіти ────────────────────────────────────────────────────────────────────
# Контрактна година — початок комерційної доби (07:00 → 07:00). Глобальне
# налаштування для звітів (нічні витрати, годинні тренди). Фронтенд читає
# її через GET /config. За замовчуванням 7.
CONTRACT_HOUR=7

# ── Логи ─────────────────────────────────────────────────────────────────────
# LOG_DIR — шлях УСЕРЕДИНІ контейнера. Через bind-mount `.:/app` він
# відповідає <repo>/logs на хості, тож backend.log / backend.error.log /
# frontend.log — звичайні файли на сервері (читаються без Docker). Ротація 10 МБ × 5.
LOG_DIR=/app/logs
LOG_LEVEL=INFO
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
```

> **JWT_SECRET** — згенерувати: `openssl rand -hex 32`
>
> Креди БД та `CONTRACT_HOUR` читаються лише з `.env.v2` — у `docker-compose.yml`
> секретів немає. Після зміни `.env.v2` застосовуйте через **`docker compose up -d`**
> (перестворення контейнера) — `docker restart` НЕ перечитує `env_file`.

---

## 4. Запуск

```bash
cd /opt/hlviewer/HLViewer

# Одноразово на новому сервері: створити named volume для даних БД.
# Він зовнішній (external), тому compose його не видаляє навіть при `down -v`.
docker volume create hlviewer_pgdata_v2

# Перший білд: стягує базові образи з Docker Hub (~5–10 хв)
docker compose up -d --build

# Стежимо за запуском
docker compose logs -f
```

Очікувана послідовність у логах:

1. `postgres_db_v2` → `database system is ready to accept connections`
2. `fastapi_app_v2` → `alembic upgrade head` → `preload_db` → `Application startup complete`
3. `scheduler_v2` → `Scheduler started`
4. `frontend_app_v2` → `nginx: start worker process`

---

## 5. Перевірка

```bash
docker compose ps
# Бекенд живий + показує контрактну годину: {"contract_hour":7}
curl http://localhost:8001/config
curl -I http://localhost:3001
```

Відкрити у браузері: `http://<IP-сервера>:3001`

---

## 6. Підключення до шари АС4 (hostlib-файли)

Детально: [admin_access_setup.md](admin_access_setup.md)

Коротко: монтуємо мережеву шару як `/mnt/as4`, прописуємо volume у `docker-compose.yml` для сервісів `fastapi_app_v2` та `scheduler_v2`:

```yaml
volumes:
  - .:/app
  - /mnt/as4:/mnt/as4
```

Після цього в `.env.v2`:

```env
HOSTLIB_PATH=/mnt/as4/
```

Перевірка:

```bash
docker exec fastapi_app_v2 ls /mnt/as4/
```

---

## 7. Доступ контейнерів до внутрішнього REST API

DPD API (`rest-direct.zp.iot.grmu.com.ua`) знаходиться у внутрішній мережі. Docker-контейнери
за замовчуванням використовують DNS `8.8.8.8` і можуть не резолвити внутрішні імена.

**Крок 1 — перевірити резолвінг імені на хості:**

```bash
nslookup rest-direct.BRANCH.iot.grmu.com.ua
curl -k https://rest-direct.BRANCH.iot.grmu.com.ua/api/v1/
```

Якщо хост резолвить ім'я, але контейнер — ні, причина у DNS.

**Крок 2 — знайти внутрішній DNS-сервер хоста:**

```bash
cat /etc/resolv.conf   # рядок "nameserver X.X.X.X"
```

**Варіант А — передати DNS контейнерам через Docker daemon:**

```bash
nano /etc/docker/daemon.json
```

```json
{
  "dns": ["<INTERNAL_DNS_IP>", "8.8.8.8"]
}
```

```bash
systemctl restart docker
docker compose up -d
```

**Варіант Б — прописати IP напряму у `docker-compose.yml`** (якщо внутрішній IP відомий):

```yaml
fastapi_v2:
  extra_hosts:
    - "rest-direct.BRANCH.iot.grmu.com.ua:<API_IP>"
    - "auth-direct.BRANCH.iot.grmu.com.ua:<API_IP>"
```

Перевірка з середини контейнера:

```bash
docker exec fastapi_app_v2 curl -k https://rest-direct.BRANCH.iot.grmu.com.ua/api/v1/
```

---

## 8. Резервне копіювання БД

> Дані БД лежать на **зовнішньому named volume `hlviewer_pgdata_v2`** (див. п.4).
> Він не видаляється навіть при `docker compose down -v`, тому перестворення
> контейнерів (`up -d`) безпечне. Перед великими операціями однаково робіть дамп.

```bash
# Дамп
docker exec postgres_db_v2 \
  pg_dump -U YOUR_DB_USER hostlib_db_v2 | gzip > backup_$(date +%Y%m%d).sql.gz

# Відновлення
gunzip -c backup_20260101.sql.gz | \
  docker exec -i postgres_db_v2 psql -U YOUR_DB_USER hostlib_db_v2
```

---

## 9. Логи

Є два рівні логів.

### 9.1. Файлові логи застосунку (не залежать від Docker)

Завдяки bind-mount `.:/app` бекенд і scheduler пишуть у `LOG_DIR=/app/logs`, що на
хості відповідає **`<repo>/HLViewer/logs/`**. Це звичайні файли — читаються без Docker:

| Файл | Вміст |
|------|-------|
| `backend.log` | усе від рівня `LOG_LEVEL` (INFO) і вище, разом з access-логом uvicorn |
| `backend.error.log` | лише `ERROR` і вище — швидкий розбір падінь (трейсбеки) |
| `frontend.log` | помилки з браузера (через `POST /api/client-log`) |

```bash
cd /opt/hlviewer/HLViewer
tail -f logs/backend.error.log      # стежити за помилками бекенда в реальному часі
tail -n 200 logs/frontend.log       # останні помилки фронтенду
```

- **Ротація:** кожен файл до `LOG_MAX_BYTES` (10 МБ), зберігається `LOG_BACKUP_COUNT`
  (5) архівів → максимум ~50 МБ на файл. Multiprocess-safe (8 воркерів пишуть в один
  файл без гонок завдяки `concurrent-log-handler`).
- Всі необроблені винятки логуються з повним трейсбеком у `backend.error.log`.
- Помилки JS у браузері (`window.onerror`, `unhandledrejection`, React ErrorBoundary)
  автоматично летять у `frontend.log`.

### 9.2. stdout-логи контейнерів (через Docker)

Вивід процесів і **access/error-логи nginx фронтенда** (у фронтенда bind-mount коду
немає) дивляться через Docker; вони ротуються драйвером `json-file` (10 МБ × 5):

```bash
docker compose logs -f fastapi_v2     # бекенд
docker compose logs -f frontend_v2    # nginx фронтенда
docker compose logs --tail 100        # всі сервіси
```

> ⚠️ `logs/` у `.gitignore` — лог-файли НЕ комітяться. На сервері вони створюються
> автоматично при першому старті.
