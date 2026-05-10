# Розгортання HLViewer на Ubuntu без Docker

---

## Необхідне ПЗ на сервері

| ПЗ | Версія | Призначення |
|---|---|---|
| Python | 3.11 | бекенд |
| PostgreSQL | 15 (або 14) | база даних |
| nginx | будь-яка актуальна | роздача фронтенду + проксі |

Python 3.11 — потрібна саме ця версія (або 3.10+). Перевірити що є: `python3 --version`.

---

## Архітектура

```
[Браузер] → nginx:80 → [React /var/www/hlviewer_front_front]
                      → proxy → [uvicorn:8001]  ← FastAPI
[uvicorn] → [PostgreSQL:5432]
[scheduler_runner] → [PostgreSQL:5432]  ← APScheduler, оновлення кожні 30 хв
```

---

## 1. Підготовка на локальній машині (Windows)

### 1.1. Python wheels для Linux

Пакети в `packages/` зібрані під Windows — для Ubuntu потрібні Linux wheels.

На машині з інтернетом і Linux (або WSL):
```bash
pip download -r requirements.txt \
    --platform linux_x86_64 \
    --python-version 311 \
    --only-binary=:all: \
    --dest packages_linux/
```

Якщо деякі пакети не мають binary wheel — без прапорців:
```bash
pip download -r requirements.txt --dest packages_linux/
```

### 1.2. Що переносити на сервер

`dist/` (зібраний React) вже закомічена у `front_repo` — окремо збирати і переносити не потрібно.

```
hlviewer-deploy/
├── back_repo/        # bare git репозиторій бекенду (D:\Projects\HLViewer\HLViewer\back_repo\)
├── front_repo/       # bare git репозиторій фронтенду (D:\Projects\HLViewer\frontend\front_repo\)
└── packages_linux/   # Linux wheels
```

---

## 2. Копіювання на сервер

З Windows на Ubuntu (без інтернету на сервері):

**scp з PowerShell:**
```powershell
scp -r D:\Projects\HLViewer\hlviewer-deploy\ user@192.168.1.100:/tmp/
```

**WinSCP** — GUI, підключення SFTP на порт 22, перетягнути мишкою.

**RDP drive sharing** — при підключенні через RDP увімкнути "Локальні диски", на сервері доступний `/mnt/tsclient/D/...`

**USB / мережева папка** — будь-який зручний спосіб.

---

## 3. Розгортання на сервері

### 3.1. Структура папок

```bash
sudo mkdir -p /opt/backend
sudo chown $USER:$USER /opt/backend

# Папки для bare-репозиторіїв (звідси робитиметься git clone/pull)
mkdir -p /opt/repos

# Робочі папки
mkdir -p /opt/backend/backend/data/askcfgs
mkdir -p /opt/backend/hostlibs
mkdir -p /opt/backend/logs
```

### 3.2. Скопіювати bare-репозиторії

```bash
cp -r /tmp/hlviewer-deploy/back_repo/  /opt/repos/back_repo
cp -r /tmp/hlviewer-deploy/front_repo/ /opt/repos/front_repo
```

### 3.3. Клонувати код з bare-репозиторіїв

```bash
# Бекенд
git clone /opt/repos/back_repo /opt/backend
# або якщо папка вже існує:
cd /opt/backend && git init && git remote add origin /opt/repos/back_repo && git pull origin master

# Фронтенд (якщо будуєте React на сервері)
git clone /opt/repos/front_repo /opt/frontend
```

### 3.4. Надалі — оновлення коду з репо

```bash
# Бекенд
cd /opt/backend
git pull origin master          # або потрібна гілка, наприклад feature/grmu-branch-db-config

# Фронтенд
cd /opt/frontend
git pull origin master
```

### 3.3. Virtualenv та залежності

```bash
cd /opt/backend
python3.11 -m venv .venv
source .venv/bin/activate

pip install --no-index --find-links=/tmp/hlviewer-deploy/packages_linux/ -r requirements.txt
```

### 3.4. Налаштувати .env

```bash
cp /opt/backend/.env.v2 /opt/backend/.env.production
nano /opt/backend/.env.production
```

```env
POSTGRES_DB=hostlib_db
POSTGRES_USER=hlviewer
POSTGRES_PASSWORD=НАДІЙНИЙ_ПАРОЛЬ
DB_HOST=localhost
DB_PORT=5432
DATABASE_URL=postgresql+asyncpg://hlviewer:НАДІЙНИЙ_ПАРОЛЬ@localhost:5432/hostlib_db

JWT_SECRET=МІНІМУМ_32_ВИПАДКОВИХ_СИМВОЛИ
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ПАРОЛЬ_АДМІНА
DEFAULT_USERNAME=viewer
DEFAULT_PASSWORD=ПАРОЛЬ_VIEWER
AUTO_LOGIN=false

CORS_ORIGINS=http://ВАШ_IP
TZ=Europe/Kyiv
DEBUG=false
```

### 3.5. PostgreSQL — створити БД

```bash
sudo -u postgres psql
```
```sql
CREATE USER hlviewer WITH PASSWORD 'НАДІЙНИЙ_ПАРОЛЬ';
CREATE DATABASE hostlib_db OWNER hlviewer;
\q
```

### 3.6. Міграції та початкові дані

```bash
cd /opt/backend
source .venv/bin/activate
export $(grep -v '^#' .env.production | xargs)

cd backend && alembic upgrade head && cd ..
python -m backend.db.preload_db.preload_db
```

### 3.7. Перевірити вручну

```bash
uvicorn backend.api.main:app --host 0.0.0.0 --port 8001
# Відкрити http://ВАШ_IP:8001/docs — має відповісти
# Ctrl+C
```

---

## 4. systemd сервіси

### FastAPI (`/etc/systemd/system/hlviewer-api.service`)

```ini
[Unit]
Description=HLViewer FastAPI
After=network.target postgresql.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/backend
EnvironmentFile=/opt/backend/.env.production
ExecStart=/opt/backend/.venv/bin/uvicorn backend.api.main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5
StandardOutput=append:/opt/backend/logs/api.log
StandardError=append:/opt/backend/logs/api.log

[Install]
WantedBy=multi-user.target
```

### Scheduler (`/etc/systemd/system/hlviewer-scheduler.service`)

```ini
[Unit]
Description=HLViewer Scheduler
After=hlviewer-api.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/backend
EnvironmentFile=/opt/backend/.env.production
ExecStart=/opt/backend/.venv/bin/python -m backend.hl_engine.scheduler_runner
Restart=always
RestartSec=10
StandardOutput=append:/opt/backend/logs/scheduler.log
StandardError=append:/opt/backend/logs/scheduler.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable hlviewer-api hlviewer-scheduler
sudo systemctl start hlviewer-api hlviewer-scheduler
```

---

## 5. Фронтенд — nginx

### Скопіювати React

`dist/` вже є у склонованому `front_repo`:

```bash
sudo mkdir -p /var/www/hlviewer_front
sudo cp -r /opt/frontend/react-frontend/dist/. /var/www/hlviewer_front/
sudo chown -R www-data:www-data /var/www/hlviewer_front
```

### nginx конфіг (`/etc/nginx/sites-available/hlviewer`)

```nginx
server {
    listen 80;
    server_name ВАШ_IP_АБО_ДОМЕН;

    root /var/www/hlviewer_front;
    index index.html;

    gzip on;
    gzip_types text/plain text/css application/javascript application/json;

    location ~ ^/(auth|grmu_branch|lumgs|virtual_lines|enterprise|enterprise-mappings|device-catalog)/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location ~ ^/update_data {
        proxy_pass http://127.0.0.1:8001;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    location ~ ^/(lines|gas-volume-calcs|gas-volume-calc-types|edit_counts|sys_counts|edit|edit-types|daily|daily_virtual|hourly|hourly_virtual|sys|sys-types|param|users|preload_data) {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache";
    }

    location ~* \.(js|css|png|ico|svg|woff2)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/hlviewer /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 6. Перевірка

```bash
sudo systemctl status hlviewer-api hlviewer-scheduler nginx postgresql
ss -tlnp | grep -E '80|8001|5432'
tail -f /opt/backend/logs/api.log
```

Відкрити: `http://ВАШ_IP/`

---

## 7. Оновлення

```bash
cd /opt/backend
git pull                                      # або скопіювати нові файли
source .venv/bin/activate
cd backend && alembic upgrade head && cd ..
sudo systemctl restart hlviewer-api hlviewer-scheduler

# Якщо змінився фронтенд:
sudo cp -r /tmp/new-dist/. /var/www/hlviewer_front/
```
