# Розгортання HLViewer v2 на Windows-сервері

## Структура на сервері

```
D:\Metr\
├── backend_prod\          ← бекенд (git pull сюди)
│   ├── backend\
│   ├── venv\              ← Python virtualenv
│   ├── .env               ← змінні середовища (load_dotenv() читає саме .env)
│   └── ...
├── nginx-1.28.3\
│   ├── nginx.exe
│   ├── conf\nginx.conf    ← конфіг nginx
│   └── html\              ← статичні файли фронтенду (dist\*)
└── batniki\
    ├── start_api.bat
    ├── start_nginx.bat
    └── start_scheduler.bat
```

---

## Файл `.env` на сервері

`backend/settings.py` викликає `load_dotenv()` — читає `.env` з кореня проекту.
Створити `D:\Metr\backend_prod\.env`:

```env
# ── База даних ─────────────────────────────────────────────────────────────
POSTGRES_USER=your_db_user
POSTGRES_PASSWORD=your_db_password
DB_HOST=localhost
DB_PORT=5432
POSTGRES_DB=your_db_name
DATABASE_URL=postgresql+asyncpg://your_db_user:your_db_password@localhost:5432/your_db_name

# ── Безпека ────────────────────────────────────────────────────────────────
JWT_SECRET=довгий-рандомний-рядок-мінімум-32-символи
ADMIN_USERNAME=admin
ADMIN_PASSWORD=початковий-пароль-адміна

# ── CORS (адреси з яких дозволені запити до API) ───────────────────────────
CORS_ORIGINS=http://localhost:3001,http://your-server-ip:3001

# ── Шляхи ─────────────────────────────────────────────────────────────────
HOSTLIB_PATH=D:\Metr\hostlibs
ENTERPRISE_MAPPINGS_PATH=backend/data/enterprise_mappings.xlsx

# ── Telegram (необов'язково) ───────────────────────────────────────────────
BOT_TOKEN=
CHAT_ID=

# ── Email (необов'язково) ──────────────────────────────────────────────────
EMAIL_PASSWORD=

# ── DPD API (необов'язково, є дефолти в коді) ─────────────────────────────
DPD_USERNAME=
DPD_PASSWORD=

# ── Інше ──────────────────────────────────────────────────────────────────
DEBUG=false
```

> `.env` — не в git (в `.gitignore`). При `git pull` не перезаписується.

---

## Початкове розгортання

### 1. Клонувати/скопіювати репозиторій

```bat
D:
cd D:\Metr
:: якщо перший раз — з back_repo на флешці:
git clone D:\transfer\back_repo backend_prod
:: або pull якщо вже є:
cd backend_prod
git pull D:\transfer\back_repo feature/grmu-branch-db-config
```

### 2. Virtualenv і залежності

```bat
cd D:\Metr\backend_prod
python -m venv venv
call venv\Scripts\activate
pip install -r requirements.prod.txt
```

### 3. Створити `.env` (див. вище)

### 4. Міграції БД

```bat
call venv\Scripts\activate
set PYTHONPATH=D:\Metr\backend_prod
cd D:\Metr\backend_prod\backend\db
alembic upgrade head
```

### 5. Скопіювати фронтенд в nginx

```bat
xcopy /E /Y D:\transfer\dist\* D:\Metr\nginx-1.28.3\html\
```

### 6. Запустити батники (кожен в окремому CMD-вікні)

1. `start_api.bat`
2. `start_nginx.bat`
3. `start_scheduler.bat`

---

## Батники

### start_api.bat
```bat
@echo off
title HLViewer v2 - API
D:
cd D:\Metr\backend_prod
call venv\Scripts\activate
set PYTHONPATH=D:\Metr\backend_prod
uvicorn backend.api.main:app --host 0.0.0.0 --port 8001
pause
```

### start_nginx.bat
```bat
@echo off
title HLViewer v2 - Nginx
D:
cd D:\Metr\nginx-1.28.3
nginx.exe -t
if errorlevel 1 (
    echo Config error.
    pause
    exit /b 1
)
taskkill /IM nginx.exe /F >nul 2>&1
nginx.exe
echo Nginx started on port 3001.
pause
```

### start_scheduler.bat
```bat
@echo off
title HLViewer v2 - Scheduler
D:
cd D:\Metr\backend_prod
call venv\Scripts\activate
set PYTHONPATH=D:\Metr\backend_prod
python -m backend.hl_engine.scheduler_runner
pause
```

---

## Конфігурація портів

### Порт API

В `start_api.bat` — параметр `--port`:
```bat
uvicorn backend.api.main:app --host 0.0.0.0 --port 8001
```
Після зміни — оновити `CORS_ORIGINS` у `.env` і проксі в `nginx.conf`.

### Порт фронтенду

В `nginx-1.28.3\conf\nginx.conf`:
```nginx
server {
    listen 3001;           ← змінити тут

    location ~ ^/(auth|grmu_branch|...) {
        proxy_pass http://127.0.0.1:8001;   ← має збігатись з портом API
    }
}
```
Після зміни — перезапустити `start_nginx.bat`.

---

## Оновлення бекенду

```bat
:: 1. Скопіювати back_repo з флешки в D:\transfer\back_repo

:: 2. Підтягнути зміни
cd D:\Metr\backend_prod
git pull D:\transfer\back_repo feature/grmu-branch-db-config

:: 3. Оновити залежності (якщо змінились)
call venv\Scripts\activate
pip install -r requirements.prod.txt

:: 4. Міграції (якщо були нові)
set PYTHONPATH=D:\Metr\backend_prod
cd backend\db
alembic upgrade head
cd ..\..

:: 5. Перезапустити start_api.bat і start_scheduler.bat
```

## Оновлення фронтенду

```bat
:: 1. Скопіювати dist\ з флешки
xcopy /E /Y D:\transfer\dist\* D:\Metr\nginx-1.28.3\html\

:: 2. Перезапустити start_nginx.bat
```

---

## Перевірка роботи

| Що перевірити | Як |
|---|---|
| API запущений | `http://localhost:8001/docs` |
| Фронтенд доступний | `http://localhost:3001` |
| БД підключена | API повертає дані без помилок |
| Scheduler працює | В консолі вікна scheduler видно логи запусків |

---

## Типові проблеми

**API не стартує — `ModuleNotFoundError`**
```bat
:: Перевірити PYTHONPATH
set PYTHONPATH=D:\Metr\backend_prod
python -c "import backend.api.main"
```

**nginx: порт зайнятий**
```bat
taskkill /IM nginx.exe /F
:: потім start_nginx.bat знову
```

**Alembic: "Target database is not up to date"**
```bat
cd D:\Metr\backend_prod\backend\db
alembic upgrade head
```

**Фронтенд показує старі файли**
- Очистити кеш браузера (Ctrl+Shift+Del → Зображення і файли)
- Перевірити що `xcopy` успішно скопіював нові файли в `html\`
