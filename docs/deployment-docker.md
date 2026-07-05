# HLViewer — Розгортання через Docker

Повна інструкція розгортання системи на сервері з Docker.

> **Кому цей документ.** Адміністратору сервера, який піднімає HLViewer «з нуля».
> Після успішного розгортання переходьте до [initial-setup.md](initial-setup.md) — там
> первинне налаштування філій, ЛУМГ, ліній, DPD-промисловості тощо через адмін-панель.

---

## 1. Передумови на сервері

```bash
apt update && apt install -y docker.io docker-compose-v2 git
```

Перевірка версій:

```bash
docker --version        # очікується Docker 24+
docker compose version  # очікується v2.x (саме "docker compose", не "docker-compose")
git --version
```

---

## 2. Отримати файли проєкту

Клонуємо обидва репозиторії (потрібен доступ до інтернету на сервері):

```bash
mkdir -p /opt/hlviewer
cd /opt/hlviewer

# Бекенд (FastAPI + БД + scheduler) і фронтенд (React, зібраний dist уже в репо)
git clone https://github.com/volodymyr-vereshchak/HLViewer.git  HLViewer
git clone https://github.com/volodymyr-vereshchak/frontend.git   frontend
```

---

## 3. Налаштування `.env.v2`

Усі секрети та параметри читаються з `.env.v2` у корені бекенду. У `docker-compose.yml`
секретів немає — тільки посилання на цей файл.

```bash
nano /opt/hlviewer/HLViewer/.env.v2
```

Вставити вміст (замінити значення, позначені `ЗАМІНИТИ` / `YOUR_...`):

```env
# ── База даних ────────────────────────────────────────────────────────────────
POSTGRES_DB=hostlib_db_v2
POSTGRES_USER=YOUR_DB_USER
POSTGRES_PASSWORD=YOUR_DB_PASSWORD
# DB_HOST / DB_PORT нижче — для доступу до БД з ХОСТА (порт 5434 проброшено назовні).
# УСЕРЕДИНІ контейнерів docker-compose.yml ПЕРЕВИЗНАЧАЄ їх на db_v2:5432 (ім'я сервісу
# в мережі Docker), тож бекенд у контейнері ходить до сервісу db_v2 — міняти не треба.
DB_HOST=localhost
DB_PORT=5434

# ── Автентифікація ────────────────────────────────────────────────────────────
# JWT_SECRET — обов'язковий, застосунок не стартує без нього (fail-closed).
# Згенерувати: openssl rand -hex 32
JWT_SECRET=ЗАМІНИТИ_openssl_rand_-hex_32
# Адмін-акаунт — авто-сідується при старті. Має доступ до адмін-панелі.
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ЗАМІНИТИ_ПАРОЛЬ
# Viewer-акаунт — авто-сідується при старті з роллю `viewer`. Viewer БЕЗ призначених
# філій бачить УСІ філії; щоб обмежити — призначте філії в адмін-панелі (вкладка
# «Користувачі»). Стара роль `viewer_all` автоматично мігрує в `viewer` при старті.
DEFAULT_USERNAME=viewer
DEFAULT_PASSWORD=ЗАМІНИТИ_ПАРОЛЬ
# AUTO_LOGIN — значення залежить від LDAP_ENABLED (див. блок LDAP нижче):
#  • LDAP вимкнено: true = автоматичний вхід як DEFAULT_USERNAME без вікна логіну
#    (зручно для внутрішнього стенду); false = звичайне вікно входу.
#  • LDAP увімкнено: кіоск-автовхід НЕ працює (всі бачать вікно входу); true/false
#    керує лише тим, як реєструються НОВІ доменні користувачі (див. нижче).
AUTO_LOGIN=true

# ── LDAP (доменна автентифікація) — необов'язково ────────────────────────────
# LDAP_ENABLED=true — користувачі можуть входити СВОЇМИ доменними логіном/паролем.
# Пароль перевіряється прямим bind'ом у Active Directory як username@LDAP_DOMAIN —
# сервісний акаунт не потрібен, групи AD не читаються. Права завжди беруться з БД
# застосунку (роль + філії призначає адмін у вкладці «Користувачі»).
# Локальні паролі (admin, viewer тощо) працюють ПАРАЛЕЛЬНО — LDAP їх не вимикає.
#
# Матриця режимів для НОВОГО доменного користувача (немає в БД застосунку):
#  LDAP_ENABLED=true + AUTO_LOGIN=true  → авто-створюється АКТИВНИМ viewer'ом без
#    обмежень по філіях і одразу входить (адмін потім може звузити доступ).
#  LDAP_ENABLED=true + AUTO_LOGIN=false → створюється НЕАКТИВНИМ; на формі
#    «Обліковий запис створено. Зверніться до адміністратора» — адмін активує
#    його і призначає роль/філії у вкладці «Користувачі» (див. initial-setup.md §2).
# Існуючий запис у БД завжди має пріоритет: деактивований користувач НЕ ввійде
# навіть із вірним доменним паролем.
#
# Значення LDAP_SERVER/LDAP_DOMAIN можна отримати скриптом scripts/get-ldap-info.bat,
# запущеним на будь-якій доменній Windows-машині.
LDAP_ENABLED=false
# Адреса контролера домену: ldap://dc.example.local або ldaps://dc.example.local:636
LDAP_SERVER=
# UPN-суфікс домену (те, що після @ у доменному логіні), напр. example.local
LDAP_DOMAIN=
# true — з'єднання по LDAPS (порт 636); false — звичайний LDAP (порт 389)
LDAP_USE_SSL=false

# ── CORS ─────────────────────────────────────────────────────────────────────
# Список дозволених джерел фронтенду. Додайте сюди IP/домен сервера з портом 3001.
CORS_ORIGINS=http://localhost:3001,http://127.0.0.1:3001,http://<IP-СЕРВЕРА>:3001

# ── Звіти ────────────────────────────────────────────────────────────────────
# Контрактна година — початок комерційної доби (напр. 07:00 → 07:00 наступного дня).
# Глобальний параметр для звітів (нічні витрати, годинні тренди). Фронтенд читає його
# через GET /config. За замовчуванням 7.
CONTRACT_HOUR=7

# ── Логи ─────────────────────────────────────────────────────────────────────
# LOG_DIR — шлях УСЕРЕДИНІ контейнера. Через bind-mount `.:/app` він відповідає
# <repo>/logs на хості, тож backend.log / backend.error.log / frontend.log —
# звичайні файли на сервері (читаються без Docker). Ротація 10 МБ × 5.
LOG_DIR=/app/logs
LOG_LEVEL=INFO
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5

# ── Промисловість (DPD) — необов'язково ───────────────────────────────────────
# Макс. одночасних з'єднань до DPD API при опитуванні промисловості (за замовч. 10).
# Підвищуйте (напр. 20–30), якщо звіти по промисловості будуються повільно: низька
# межа серіалізує запити до приладів «хвилями» і збільшує загальний час.
DPD_MAX_CONCURRENCY=10

# ── HTTPS — необов'язково ─────────────────────────────────────────────────────
# COOKIE_SECURE=true — надсилати auth-cookie лише по HTTPS. Для внутрішнього HTTP
# лишайте false, інакше cookie не встановиться і вхід не працюватиме.
COOKIE_SECURE=false
```

> **Після зміни `.env.v2`** застосовуйте через **`docker compose up -d`** (перестворення
> контейнера) — `docker restart` НЕ перечитує `env_file`.

---

## 4. Запуск

```bash
cd /opt/hlviewer/HLViewer

# Одноразово на новому сервері: створити named volume для даних БД.
# Він зовнішній (external), тому compose його НЕ видаляє навіть при `down -v` —
# дані БД переживають перестворення контейнерів.
docker volume create hlviewer_pgdata_v2

# Перший білд: стягує базові образи з Docker Hub і збирає застосунок (~5–10 хв).
# Команда запуску в контейнері: alembic upgrade head → preload_db → uvicorn (8 воркерів).
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
# Фронтенд віддає index.html
curl -I http://localhost:3001
```

Відкрити у браузері: `http://<IP-сервера>:3001`

---

## 6. Підключення до шари АС4 (архівні hostlib-файли)

Монтуємо мережеву шару як `/mnt/as4` і прописуємо volume у `docker-compose.yml` для
сервісів `fastapi_v2` та `scheduler_v2`:

```yaml
volumes:
  - .:/app
  - /mnt/as4:/mnt/as4
```

Перевірка, що контейнер бачить шару:

```bash
docker exec fastapi_app_v2 ls /mnt/as4/
```

---

## 7. Доступ контейнерів до внутрішнього REST API (DPD)

DPD API (напр. `rest-direct.zp.iot.grmu.com.ua`) знаходиться у внутрішній мережі.
Docker-контейнери за замовчуванням використовують DNS `8.8.8.8` і можуть не резолвити
внутрішні імена.

> ℹ️ URL-адреси DPD API (base URL та auth URL) і облікові дані тепер задаються **окремо
> для кожного філіалу** в адмін-панелі → вкладка «DPD Креденшали», а НЕ через env-змінні.
> Порада щодо DNS нижче лишається актуальною: контейнери мають резолвити ці внутрішні імена.

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
> Він не видаляється навіть при `docker compose down -v`, тому перестворення контейнерів
> (`up -d`) безпечне. Перед великими операціями однаково робіть дамп.

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

stdout-логи контейнерів дивляться через Docker — це найповніше джерело (вивід процесів
бекенда/scheduler + access/error-логи nginx фронтенда). Ротуються драйвером `json-file`
(10 МБ × 5):

```bash
docker compose logs -f fastapi_v2     # бекенд
docker compose logs -f frontend_v2    # nginx фронтенда
docker compose logs -f scheduler_v2   # планувальник оновлень
docker compose logs --tail 100        # всі сервіси
```
