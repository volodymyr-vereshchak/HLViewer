# Розгортання HLViewer на Ubuntu без Docker

Повна інструкція розгортання бекенду, фронтенду та бази даних на Ubuntu Server (22.04 / 24.04) без інтернету та Docker.

---

## Архітектура

```
[Браузер] → nginx:80 → [React static files]
                      → [FastAPI uvicorn:8001]
                      → [Scheduler (APScheduler)]
[FastAPI] → [PostgreSQL:5432]
```

Три процеси на сервері:
- **uvicorn** — FastAPI бекенд
- **scheduler_runner** — APScheduler (оновлення даних кожні 30 хв)
- **nginx** — роздача React SPA + проксі до бекенду

---

## 1. Підготовка на локальній машині

### 1.1. Зібрати Linux-колеса (wheels) для Python-залежностей

> **Важливо:** Колеса в папці `packages/` зібрані під Windows. Для Ubuntu потрібні Linux-колеса.

На Linux-машині або у WSL:

```bash
pip download -r requirements.txt \
    --platform linux_x86_64 \
    --python-version 311 \
    --only-binary=:all: \
    --dest packages_linux/
```

Якщо деякі пакети не мають binary wheel — завантажити source:
```bash
pip download -r requirements.txt --dest packages_linux/
```

### 1.2. Зібрати React-додаток

На локальній машині:
```bash
cd frontend/react-frontend
npm ci --legacy-peer-deps
npm run build
# Результат: frontend/react-frontend/dist/
```

### 1.3. Підготувати архіви для копіювання на сервер

```
hlviewer-deploy/
├── backend-repo/        # git bare clone або zip бекенду
├── packages_linux/      # Linux wheels
├── react-dist/          # зібраний React (dist/)
└── postgres-15.tar      # (опціонально, якщо PostgreSQL теж офлайн)
```

---

## 2. Копіювання файлів на сервер

> Якщо Ubuntu без GUI і доступ через Remote Desktop — оберіть один з варіантів нижче.

### Варіант А — scp з Windows PowerShell (рекомендовано)

На Windows 10/11 OpenSSH вже вбудований. Відкрити PowerShell і виконати:

```powershell
# Скопіювати всю папку
scp -r D:\Projects\HLViewer\hlviewer-deploy\ user@192.168.1.100:/tmp/

# Або окремі архіви
scp D:\Projects\HLViewer\hlviewer-deploy.tar.gz user@192.168.1.100:/tmp/
```

Якщо SSH на нестандартному порту:
```powershell
scp -P 2222 -r D:\Projects\HLViewer\hlviewer-deploy\ user@192.168.1.100:/tmp/
```

На сервері розпакувати:
```bash
cd /tmp && tar -xzf hlviewer-deploy.tar.gz
```

### Варіант Б — WinSCP (GUI, найзручніший)

1. Встановити **WinSCP** з [winscp.net](https://winscp.net) (є portable версія без інсталяції)
2. Підключитись: протокол **SFTP**, хост — IP сервера, порт 22, логін/пароль
3. Перетягнути папку `hlviewer-deploy/` мишкою з лівої панелі (Windows) у праву (Ubuntu `/tmp/`)

### Варіант В — через RDP drive sharing

Якщо підключаєтесь до Ubuntu через RDP (xrdp):

1. У Windows клієнті RDP перед підключенням:
   **"Параметри"** → вкладка **"Локальные ресурсы"** → **"Подробнее"**
   → поставити галочку на диску `D:` (або потрібному)
2. Підключитись до Ubuntu через RDP
3. У терміналі Ubuntu диск буде доступний:
```bash
ls /mnt/tsclient/
# D/   ← ваш Windows диск D:

cp -r "/mnt/tsclient/D/Projects/HLViewer/hlviewer-deploy/" /tmp/
```

### Варіант Г — мережева папка (SMB)

Якщо Ubuntu і Windows в одній мережі:

```bash
# На Ubuntu встановити cifs-utils (якщо немає):
sudo apt install -y cifs-utils

# Підмонтувати Windows шару
sudo mkdir -p /mnt/winshare
sudo mount -t cifs //192.168.1.50/share /mnt/winshare \
    -o username=WINDOWS_USER,password=ПАРОЛЬ,uid=$(id -u),gid=$(id -g)

# Скопіювати файли
cp -r /mnt/winshare/hlviewer-deploy/ /tmp/

# Відмонтувати
sudo umount /mnt/winshare
```

### Варіант Д — USB-носій

```bash
# Знайти USB пристрій
lsblk
# sdb1 — зазвичай перша флешка

sudo mkdir -p /mnt/usb
sudo mount /dev/sdb1 /mnt/usb
cp -r /mnt/usb/hlviewer-deploy/ /tmp/
sudo umount /mnt/usb
```

---

## 3. Встановлення ПЗ на сервері

### 3.1. Python 3.11

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa   # якщо Ubuntu < 24.04
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

Перевірка:
```bash
python3.11 --version  # Python 3.11.x
```

### 3.2. PostgreSQL 15

```bash
sudo apt install -y postgresql postgresql-client
# або конкретна версія:
sudo apt install -y postgresql-15
```

Запуск і автозапуск:
```bash
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### 3.3. nginx

```bash
sudo apt install -y nginx
sudo systemctl start nginx
sudo systemctl enable nginx
```

### 3.4. Node.js 20 (тільки якщо збираєте React на сервері)

```bash
# Офлайн: завантажити .deb пакет з nodejs.org і встановити:
sudo dpkg -i nodejs_20.x.x_amd64.deb
```

Якщо React вже зібраний локально — Node.js на сервері не потрібен.

---

## 4. Налаштування PostgreSQL

```bash
sudo -u postgres psql
```

```sql
CREATE USER hlviewer WITH PASSWORD 'ВАШ_ПАРОЛЬ';
CREATE DATABASE hostlib_db OWNER hlviewer;
\q
```

Перевірка підключення:
```bash
psql -U hlviewer -d hostlib_db -h localhost
```

---

## 5. Розгортання бекенду

### 5.1. Структура папок

```bash
sudo mkdir -p /opt/hlviewer
sudo chown $USER:$USER /opt/hlviewer
mkdir -p /opt/hlviewer/backend
mkdir -p /opt/hlviewer/logs
mkdir -p /opt/hlviewer/backend/data/askcfgs
mkdir -p /opt/hlviewer/hostlibs
```

### 5.2. Розпакувати код бекенду

```bash
# Варіант: через git bare repo
cd /opt/hlviewer
git clone /tmp/hlviewer-deploy/backend-repo .

# Або: розпакувати zip
unzip /tmp/hlviewer-deploy/backend.zip -d /opt/hlviewer/
```

### 5.3. Створити virtualenv та встановити залежності

```bash
cd /opt/hlviewer
python3.11 -m venv .venv
source .venv/bin/activate

# Встановити з локальних wheels (без інтернету)
pip install --no-index --find-links=/tmp/hlviewer-deploy/packages_linux/ -r requirements.txt
```

Перевірка:
```bash
pip list | grep fastapi
pip list | grep uvicorn
```

### 5.4. Налаштувати .env файл

```bash
cp /opt/hlviewer/.env.v2 /opt/hlviewer/.env.production
nano /opt/hlviewer/.env.production
```

Заповнити:
```env
POSTGRES_DB=hostlib_db
POSTGRES_USER=hlviewer
POSTGRES_PASSWORD=ВАШ_ПАРОЛЬ
DB_HOST=localhost
DB_PORT=5432
DATABASE_URL=postgresql+asyncpg://hlviewer:ВАШ_ПАРОЛЬ@localhost:5432/hostlib_db

JWT_SECRET=ДОВГИЙ_ВИПАДКОВИЙ_РЯДОК_МИН_32_СИМВОЛИ
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ВАШИЙ_ПАРОЛЬ_АДМІНА

DEFAULT_USERNAME=viewer
DEFAULT_PASSWORD=ВАШИЙ_ПАРОЛЬ_VIEWER
AUTO_LOGIN=false

CORS_ORIGINS=http://ВАШ_IP,http://ВАШ_ДОМЕН

TZ=Europe/Kyiv
BOT_TOKEN=                    # якщо є Telegram бот
EMAIL_PASSWORD=               # якщо є email нотифікації
DEBUG=false
```

### 5.5. Запустити міграції та preload

```bash
cd /opt/hlviewer
source .venv/bin/activate

# Міграції
cd backend
alembic upgrade head
cd ..

# Початкове завантаження довідників (FLOWTYPE, SYSNAME, EDITNAME)
python -m backend.db.preload_db.preload_db
```

### 5.6. Перевірити запуск uvicorn вручну

```bash
cd /opt/hlviewer
source .venv/bin/activate

export $(grep -v '^#' .env.production | xargs)

uvicorn backend.api.main:app --host 0.0.0.0 --port 8001
# Ctrl+C після перевірки
```

Відкрити в браузері: `http://ВАШ_IP:8001/docs`

---

## 6. systemd сервіси

### 6.1. Uvicorn (FastAPI)

```bash
sudo nano /etc/systemd/system/hlviewer-api.service
```

```ini
[Unit]
Description=HLViewer FastAPI backend
After=network.target postgresql.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/hlviewer
EnvironmentFile=/opt/hlviewer/.env.production
ExecStart=/opt/hlviewer/.venv/bin/uvicorn backend.api.main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5
StandardOutput=append:/opt/hlviewer/logs/api.log
StandardError=append:/opt/hlviewer/logs/api.log

[Install]
WantedBy=multi-user.target
```

### 6.2. Scheduler (APScheduler)

```bash
sudo nano /etc/systemd/system/hlviewer-scheduler.service
```

```ini
[Unit]
Description=HLViewer Scheduler
After=hlviewer-api.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/hlviewer
EnvironmentFile=/opt/hlviewer/.env.production
ExecStart=/opt/hlviewer/.venv/bin/python -m backend.hl_engine.scheduler_runner
Restart=always
RestartSec=10
StandardOutput=append:/opt/hlviewer/logs/scheduler.log
StandardError=append:/opt/hlviewer/logs/scheduler.log

[Install]
WantedBy=multi-user.target
```

### 6.3. Активувати та запустити

```bash
sudo systemctl daemon-reload

sudo systemctl enable hlviewer-api.service
sudo systemctl enable hlviewer-scheduler.service

sudo systemctl start hlviewer-api.service
sudo systemctl start hlviewer-scheduler.service

# Перевірка
sudo systemctl status hlviewer-api.service
sudo systemctl status hlviewer-scheduler.service
```

Логи:
```bash
tail -f /opt/hlviewer/logs/api.log
tail -f /opt/hlviewer/logs/scheduler.log
```

---

## 7. Розгортання фронтенду

### 7.1. Скопіювати зібраний React

```bash
sudo mkdir -p /var/www/hlviewer
sudo cp -r /tmp/hlviewer-deploy/react-dist/* /var/www/hlviewer/
sudo chown -R www-data:www-data /var/www/hlviewer
```

### 7.2. Налаштувати nginx

```bash
sudo nano /etc/nginx/sites-available/hlviewer
```

```nginx
server {
    listen 80;
    server_name ВАШ_IP_АБО_ДОМЕН;

    root /var/www/hlviewer;
    index index.html;

    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css text/xml text/javascript application/javascript application/json;

    # FastAPI проксі
    location ~ ^/(auth|grmu_branch|lumgs|virtual_lines|enterprise|enterprise-mappings|device-catalog)/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ~ ^/update_data {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    location ~ ^/(lines|gas-volume-calcs|gas-volume-calc-types|edit_counts|sys_counts|edit|edit-types|daily|daily_virtual|hourly|hourly_virtual|sys|sys-types|param|users|preload_data) {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # SPA fallback
    location / {
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache";
    }

    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

Активувати:
```bash
sudo ln -s /etc/nginx/sites-available/hlviewer /etc/nginx/sites-enabled/
sudo nginx -t        # перевірка конфігу
sudo systemctl reload nginx
```

---

## 8. Налаштування firewall

```bash
sudo ufw allow 80/tcp
sudo ufw allow 22/tcp   # SSH
sudo ufw enable
```

Порт 8001 (FastAPI) залишити закритим — доступ тільки через nginx.

---

## 9. Оновлення коду (наступний раз)

```bash
cd /opt/hlviewer
git pull offline_repo master     # або скопіювати нові файли

source .venv/bin/activate
cd backend && alembic upgrade head && cd ..

sudo systemctl restart hlviewer-api.service
sudo systemctl restart hlviewer-scheduler.service

# Якщо змінився фронтенд:
sudo cp -r /tmp/new-dist/* /var/www/hlviewer/
```

---

## 10. Перевірка роботи

```bash
# Статус сервісів
sudo systemctl status hlviewer-api.service
sudo systemctl status hlviewer-scheduler.service
sudo systemctl status nginx
sudo systemctl status postgresql

# Порти
ss -tlnp | grep -E '80|8001|5432'

# Логи
tail -100 /opt/hlviewer/logs/api.log
```

Відкрити в браузері: `http://ВАШ_IP/`

---

## Структура фінального розгортання

```
/opt/hlviewer/
├── backend/              # код бекенду
│   ├── api/
│   ├── db/
│   │   ├── alembic/
│   │   └── preload_db/
│   │       ├── FLOWTYPE.json
│   │       ├── SYSNAME.json
│   │       └── EDITNAME.json
│   ├── data/
│   │   └── askcfgs/      # CFG файли конфігурації приладів
│   └── hl_engine/
├── hostlibs/             # архіви хостлібів
├── .venv/                # Python virtualenv
├── .env.production       # конфігурація
├── requirements.txt
└── logs/
    ├── api.log
    └── scheduler.log

/var/www/hlviewer/        # React SPA
```
