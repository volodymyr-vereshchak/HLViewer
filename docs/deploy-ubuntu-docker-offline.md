# Розгортання HLViewer на Ubuntu з Docker (офлайн)

---

## Необхідне ПЗ на сервері

| ПЗ | Версія | Призначення |
|---|---|---|
| Docker Engine | 24+ | запуск контейнерів |
| Docker Compose Plugin | v2+ | оркестрація (`docker compose`) |

Більше нічого встановлювати не потрібно — Python, PostgreSQL, nginx — все всередині контейнерів.

Перевірити що є: `docker --version`, `docker compose version`

---

## Архітектура (контейнери)

```
[Браузер] → frontend_app_v2:3001 (nginx)
                → proxy → fastapi_app_v2:8001 (uvicorn)
fastapi_app_v2  → postgres_db_v2:5432
scheduler_app_v2 → postgres_db_v2:5432
```

| Контейнер | Образ | Зовнішній порт |
|---|---|---|
| `postgres_db_v2` | postgres:15 | 5434 |
| `fastapi_app_v2` | hlviewer-fastapi-v2 | 8001 |
| `frontend_app_v2` | hlviewer-frontend-v2 | 3001 |
| `scheduler_app_v2` | hlviewer-fastapi-v2 | — |

---

## 1. Підготовка на локальній машині (Windows, з інтернетом)

### 1.1. Зібрати та експортувати Docker-образи

```bash
cd D:\Projects\HLViewer\HLViewer

# Зібрати образи (якщо ще не зібрані)
docker compose -f docker-compose.v2.yml build

# Завантажити postgres якщо немає локально
docker pull postgres:15

# Експортувати образи у файли
docker save hlviewer-fastapi-v2  -o hlviewer-fastapi-v2.tar
docker save hlviewer-frontend-v2 -o hlviewer-frontend-v2.tar
docker save postgres:15          -o postgres-15.tar
```

### 1.2. Зібрати пакет для сервера

```
hlviewer-server/
├── hlviewer-fastapi-v2.tar      # образ бекенду
├── hlviewer-frontend-v2.tar     # образ фронтенду
├── postgres-15.tar              # образ PostgreSQL
├── docker-compose.v2.yml        # compose файл
├── wait_for_it.sh               # скрипт очікування БД
├── nginx.v2.conf                # nginx конфіг фронтенду
├── .env.v2.template             # шаблон конфігурації
└── preload_db/
    ├── FLOWTYPE.json
    ├── SYSNAME.json
    └── EDITNAME.json
```

```powershell
# Скопіювати потрібні файли
$src = "D:\Projects\HLViewer\HLViewer"
$dst = "D:\hlviewer-server"
New-Item -ItemType Directory -Force $dst

Copy-Item "$src\docker-compose.v2.yml" $dst
Copy-Item "$src\wait_for_it.sh" $dst
Copy-Item "$src\.env.v2" "$dst\.env.v2.template"
Copy-Item "$src\..\frontend\react-frontend\nginx.v2.conf" $dst

New-Item -ItemType Directory -Force "$dst\preload_db"
Copy-Item "$src\backend\db\preload_db\FLOWTYPE.json" "$dst\preload_db\"
Copy-Item "$src\backend\db\preload_db\SYSNAME.json"  "$dst\preload_db\"
Copy-Item "$src\backend\db\preload_db\EDITNAME.json" "$dst\preload_db\"

Copy-Item "hlviewer-fastapi-v2.tar"  $dst
Copy-Item "hlviewer-frontend-v2.tar" $dst
Copy-Item "postgres-15.tar"          $dst
```

---

## 2. Копіювання на сервер

**scp з PowerShell:**
```powershell
scp -r D:\hlviewer-server\ user@192.168.1.100:/opt/hlviewer/
```

**WinSCP** — GUI, підключення SFTP на порт 22, перетягнути мишкою.

**RDP drive sharing** — при підключенні через RDP увімкнути "Локальні диски", на сервері:
```bash
cp -r /mnt/tsclient/D/hlviewer-server/. /opt/hlviewer/
```

**USB:**
```bash
sudo mount /dev/sdb1 /mnt/usb
cp -r /mnt/usb/hlviewer-server/. /opt/hlviewer/
sudo umount /mnt/usb
```

---

## 3. Розгортання на сервері

### 3.1. Структура папок

```bash
mkdir -p /opt/hlviewer/backend/data/askcfgs
mkdir -p /opt/hlviewer/backend/db/preload_db
mkdir -p /opt/hlviewer/hostlibs
```

```bash
# Скопіювати preload довідники
cp /opt/hlviewer/preload_db/*.json /opt/hlviewer/backend/db/preload_db/
```

### 3.2. Імпортувати Docker-образи

```bash
cd /opt/hlviewer

docker load -i postgres-15.tar
docker load -i hlviewer-fastapi-v2.tar
docker load -i hlviewer-frontend-v2.tar

# Перевірка
docker images
```

### 3.3. Налаштувати .env

```bash
cp /opt/hlviewer/.env.v2.template /opt/hlviewer/.env.v2
nano /opt/hlviewer/.env.v2
```

Обов'язково змінити:
```env
POSTGRES_DB=hostlib_db_v2
POSTGRES_USER=hlviewer
POSTGRES_PASSWORD=НАДІЙНИЙ_ПАРОЛЬ_БД

DATABASE_URL=postgresql+asyncpg://hlviewer:НАДІЙНИЙ_ПАРОЛЬ_БД@db_v2:5432/hostlib_db_v2

JWT_SECRET=МІНІМУМ_32_ВИПАДКОВИХ_СИМВОЛИ
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ПАРОЛЬ_АДМІНА
DEFAULT_USERNAME=viewer
DEFAULT_PASSWORD=ПАРОЛЬ_VIEWER
AUTO_LOGIN=false

CORS_ORIGINS=http://ВАШ_IP:3001
TZ=Europe/Kyiv
DEBUG=false
```

### 3.4. Оновити паролі в docker-compose.v2.yml

```bash
nano /opt/hlviewer/docker-compose.v2.yml
```

Змінити в секції `db_v2`:
```yaml
    environment:
      POSTGRES_DB: hostlib_db_v2
      POSTGRES_USER: hlviewer                    # ← має збігатись з .env.v2
      POSTGRES_PASSWORD: НАДІЙНИЙ_ПАРОЛЬ_БД      # ← має збігатись з .env.v2
```

І в `fastapi_v2` / `scheduler_v2`:
```yaml
    environment:
      - DATABASE_URL=postgresql+asyncpg://hlviewer:НАДІЙНИЙ_ПАРОЛЬ_БД@db_v2:5432/hostlib_db_v2
```

### 3.5. Запустити

```bash
cd /opt/hlviewer
docker compose -f docker-compose.v2.yml up -d
```

Перевірити:
```bash
docker compose -f docker-compose.v2.yml ps
docker compose -f docker-compose.v2.yml logs fastapi_v2 --tail=30
```

Очікуваний результат у логах: `Application startup complete`

Відкрити в браузері: `http://ВАШ_IP:3001/`

---

## 4. Автозапуск після ребуту

Docker restart policy `always` вже прописана в compose-файлі. Потрібно тільки щоб Docker сам запускався:

```bash
sudo systemctl enable docker
```

Або додати systemd сервіс (`/etc/systemd/system/hlviewer-docker.service`):

```ini
[Unit]
Description=HLViewer Docker Compose
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/hlviewer
ExecStart=docker compose -f docker-compose.v2.yml up -d
ExecStop=docker compose -f docker-compose.v2.yml down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable hlviewer-docker
```

---

## 5. Корисні команди

```bash
cd /opt/hlviewer

# Статус
docker compose -f docker-compose.v2.yml ps

# Логи
docker compose -f docker-compose.v2.yml logs -f fastapi_v2
docker compose -f docker-compose.v2.yml logs -f scheduler_v2

# Увійти в контейнер
docker exec -it fastapi_app_v2 bash

# Консоль PostgreSQL
docker exec -it postgres_db_v2 psql -U hlviewer -d hostlib_db_v2

# Перезапустити сервіс
docker compose -f docker-compose.v2.yml restart fastapi_v2

# Зупинити все
docker compose -f docker-compose.v2.yml down

# Запустити все
docker compose -f docker-compose.v2.yml up -d
```

---

## 6. Оновлення

На локальній машині:
```bash
docker compose -f docker-compose.v2.yml build fastapi_v2
docker save hlviewer-fastapi-v2 -o hlviewer-fastapi-v2-new.tar
```

На сервері:
```bash
docker load -i /tmp/hlviewer-fastapi-v2-new.tar
cd /opt/hlviewer
docker compose -f docker-compose.v2.yml up -d --no-deps fastapi_v2 scheduler_v2
```

Оновити довідники (SYSNAME, EDITNAME, FLOWTYPE):
```bash
cp /tmp/new/*.json /opt/hlviewer/backend/db/preload_db/
# Потім через Swagger: POST http://ВАШ_IP:8001/preload_data/
```
