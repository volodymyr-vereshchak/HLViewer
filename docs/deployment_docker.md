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
```

> **JWT_SECRET** — згенерувати: `openssl rand -hex 32`

---

## 4. Запуск

```bash
cd /opt/hlviewer/HLViewer

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
curl http://localhost:8001/health
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

```bash
# Дамп
docker exec postgres_db_v2 \
  pg_dump -U YOUR_DB_USER hostlib_db_v2 | gzip > backup_$(date +%Y%m%d).sql.gz

# Відновлення
gunzip -c backup_20260101.sql.gz | \
  docker exec -i postgres_db_v2 psql -U YOUR_DB_USER hostlib_db_v2
```
