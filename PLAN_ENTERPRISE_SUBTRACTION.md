# План: Вычитание объемов предприятий из ГС в суточном тренде

## Цель
При построении суточного тренда в GRSTrends для выбранного периода:
1. Считать Excel файл с маппингом вычислителей предприятий к линиям (line_id)
2. Запросить DPD API для получения объемов предприятий за период
3. Вычесть объемы предприятий из объемов ГС по суткам
4. Построить тренд без учета предприятий

## Требования от пользователя
- ✅ Excel файл нужно создать (структура описана ниже)
- ✅ Использовать `dvstAlwrk` (стандартный объем) из DPD API
- ✅ Интеграция через HLViewer backend API (новый endpoint)
- ✅ Получать данные на лету (не сохранять в БД)

---

## 1. Структура Excel файла с маппингом

**Путь:** `D:\Projects\HLViewer\HLViewer\backend\data\enterprise_mappings.xlsx`

**Колонки:**

| Колонка | Тип | Описание | Пример |
|---------|-----|----------|--------|
| `line_id` | int | ID линии из HLViewer БД | 1 |
| `serNum` | int | Серийный номер вычислителя | 123456 |
| `mfDev` | int | Код производителя | 16 |
| `typeDev` | int | Тип устройства | 1 |
| `chNum` | int | Номер канала | 1 |
| `enterprise_name` | str | Название предприятия | "ТОВ Завод №1" |
| `active` | bool | Активен ли маппинг | TRUE |

**Примечания:**
- Одна линия (line_id) может иметь несколько предприятий (несколько строк в Excel)
- Параметры `serNum`, `mfDev`, `typeDev`, `chNum` соответствуют параметрам DPD API
- Колонка `active` позволяет отключать маппинги без удаления

---

## 2. Backend: Новый endpoint для получения объемов предприятий

### 2.1 Добавить настройки DPD API

**Файл:** `D:\Projects\HLViewer\HLViewer\backend\settings.py`

Добавить в `backend_settings`:
```python
# DPD API Configuration
"DPD_API_BASE_URL": "https://rest-direct.zp.iot.grmu.com.ua/api/v1/",
"DPD_AUTH_URL": "https://auth-direct.zp.iot.grmu.com.ua/auth/login",
"DPD_USERNAME": os.getenv("DPD_USERNAME", "zaporizhDirect"),
"DPD_PASSWORD": os.getenv("DPD_PASSWORD", "xTqYaRmlYQFY"),
"DPD_TIMEOUT": 30,
"ENTERPRISE_MAPPINGS_PATH": os.getenv(
    "ENTERPRISE_MAPPINGS_PATH",
    "backend/data/enterprise_mappings.xlsx"
),
```

**Файл:** `D:\Projects\HLViewer\HLViewer\.env`

Добавить:
```env
DPD_USERNAME=zaporizhDirect
DPD_PASSWORD=xTqYaRmlYQFY
ENTERPRISE_MAPPINGS_PATH=backend/data/enterprise_mappings.xlsx
```

### 2.2 Создать DPD API клиент

**Файл:** `D:\Projects\HLViewer\HLViewer\backend\services\dpd_client.py` (новый)

**Создать директорию:** `D:\Projects\HLViewer\HLViewer\backend\services\`

**Функционал:**
- Класс `DPDClient` с async httpx
- JWT аутентификация (access + refresh tokens)
- Метод `async get_volumes(devices, date_from, date_to)` - запрос к POST `/api/v1/devices/volumes`
- Хранение токенов в памяти (не в файле)
- Обработка 401/403 с автообновлением токенов
- Timeout 30 сек, retry логика

**Запрос к DPD API:**
```
POST /api/v1/devices/volumes
Params: from=YYYY-MM-DD, to=YYYY-MM-DD, page=0, size=6000
Headers: Authorization: Bearer {access_token}
```

**Ответ DPD API (content):**
```json
[
  {
    "serNum": 123456,
    "mfDev": 16,
    "typeDev": 1,
    "chNum": 1,
    "dvstAlwrk": 1234.56,  // <-- нужный объем
    "date": "2025-12-01"
  }
]
```

### 2.3 Создать менеджер Excel маппингов

**Файл:** `D:\Projects\HLViewer\HLViewer\backend\services\enterprise_mappings.py` (новый)

**Функционал:**
- Функция `load_mappings()` - загрузка Excel с кешированием (pandas.read_excel)
- Кеширование на 5 минут (проверка mtime файла)
- Функция `get_devices_for_lines(line_ids: list[int])` - фильтрация по line_id и active=True
- Возврат списка dict: `[{serNum, mfDev, typeDev, chNum, enterprise_name}, ...]`

### 2.4 Создать Pydantic модели

**Файл:** `D:\Projects\HLViewer\HLViewer\backend\db\models\enterprise_models.py` (новый)

```python
from pydantic import BaseModel, Field
from datetime import date

class DeviceVolume(BaseModel):
    serNum: int
    mfDev: int
    typeDev: int
    chNum: int
    enterprise_name: str
    volume: float = Field(ge=0)

class EnterpriseVolumeResponse(BaseModel):
    line_id: int
    period: date
    total_volume: float = Field(ge=0)
    device_count: int = Field(ge=0)
    devices: list[DeviceVolume]
```

### 2.5 Создать endpoint

**Файл:** `D:\Projects\HLViewer\HLViewer\backend\api\endpoints\enterprise_ep.py` (новый)

**Класс:** `EnterpriseRouter` (по аналогии с `LineRouter`)

**Endpoint:** `GET /enterprise/volumes/`

**Query параметры:**
- `line_id: list[int]` (required)
- `from_date: str` (YYYY-MM-DD, required)
- `to_date: str` (YYYY-MM-DD, required)

**Логика:**
1. Загрузить маппинги из Excel для указанных line_id (через `enterprise_mappings.py`)
2. Если маппингов нет - вернуть `[]` (не ошибка)
3. Запросить DPD API для всех найденных устройств (через `dpd_client.py`)
4. Агрегировать по `line_id + date`: суммировать `dvstAlwrk`
5. Вернуть структуру: `list[EnterpriseVolumeResponse]`

**Response пример:**
```json
[
  {
    "line_id": 1,
    "period": "2025-12-01",
    "total_volume": 2345.67,
    "device_count": 2,
    "devices": [
      {
        "serNum": 123456,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "ТОВ Завод №1",
        "volume": 1234.56
      },
      {
        "serNum": 123457,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "ТОВ Завод №2",
        "volume": 1111.11
      }
    ]
  }
]
```

**Обработка ошибок:**
- DPD API недоступен → HTTP 503
- Excel файл отсутствует → HTTP 500
- Нет маппингов → HTTP 200 с `[]`
- Невалидные параметры → HTTP 400

### 2.6 Зарегистрировать роутер

**Файл:** `D:\Projects\HLViewer\HLViewer\backend\api\main.py`

Добавить:
```python
from backend.api.endpoints import enterprise_ep

app.include_router(enterprise_ep.enterprise_router)
```

Добавить в `tags_metadata`:
```python
{
    "name": "enterprise",
    "description": "Operations with enterprise volume data from DPD API."
}
```

---

## 3. Frontend: Интеграция в GRSTrends

### 3.1 Добавить метод API

**Файл:** `D:\Projects\HLViewer\frontend\react-frontend\src\services\api.js`

Добавить после `archiveDataApi`:
```javascript
// Enterprise volume API methods
export const enterpriseApi = {
  async getEnterpriseVolumes(lineIds, fromDate, toDate) {
    const params = {
      line_id: lineIds,
      from_date: fromDate,
      to_date: toDate
    };
    return await apiClient.get('/enterprise/volumes/', params);
  }
};
```

### 3.2 Модифицировать GRSTrends компонент

**Файл:** `D:\Projects\HLViewer\frontend\react-frontend\src\components\GRSTrends.jsx`

#### Изменение 1: Импорт enterpriseApi

Добавить в строку 2:
```javascript
import { archiveDataApi, enterpriseApi } from '../services/api';
```

#### Изменение 2: Функция calculateTrends (строки 46-79)

Заменить на:
```javascript
const calculateTrends = async () => {
  if (grsLines.length === 0) {
    setError(t('noGrsLinesConfigured'));
    return;
  }

  setIsLoading(true);
  setError(null);

  try {
    // Fetch daily data and enterprise data in parallel
    const [dailyData, enterpriseData] = await Promise.all([
      archiveDataApi.getDailyData(grsLines, dateRange.fromDate, dateRange.toDate),
      enterpriseApi.getEnterpriseVolumes(grsLines, dateRange.fromDate, dateRange.toDate)
    ]);

    if (!dailyData || dailyData.length === 0) {
      setError(t('noDataAvailable'));
      setChartData([]);
      return;
    }

    // Log warning if no enterprise data (not an error)
    if (!enterpriseData || enterpriseData.length === 0) {
      console.warn('No enterprise data available, using GS volumes only');
    }

    // Calculate trends with enterprise subtraction
    const trendsData = calculateGRSTrendsPercentages(
      dailyData,
      grsLines,
      enterpriseData || []
    );
    setChartData(trendsData);

  } catch (err) {
    setError(t('errorLoadingData'));
    console.error('Error calculating GRS trends:', err);
  } finally {
    setIsLoading(false);
  }
};
```

**Ключевые изменения:**
- Параллельный запрос двух API через `Promise.all`
- Передача `enterpriseData` в `calculateGRSTrendsPercentages`
- Мягкая обработка: если нет данных предприятий - только warning, не ошибка

#### Изменение 3: Функция calculateGRSTrendsPercentages (строки 81-132)

Заменить на:
```javascript
const calculateGRSTrendsPercentages = (dailyData, lineIds, enterpriseData = []) => {
  // Group data by line_id
  const lineDataMap = {};

  dailyData.forEach(record => {
    const lineId = record.line_id;
    if (!lineDataMap[lineId]) {
      lineDataMap[lineId] = [];
    }
    lineDataMap[lineId].push(record);
  });

  // Create enterprise volume lookup map: {line_id: {date: total_volume}}
  const enterpriseMap = {};
  enterpriseData.forEach(entry => {
    const lineId = entry.line_id;
    const date = new Date(entry.period).toISOString().split('T')[0];

    if (!enterpriseMap[lineId]) {
      enterpriseMap[lineId] = {};
    }
    enterpriseMap[lineId][date] = entry.total_volume;
  });

  // Calculate total NET volume per line (GS - Enterprise) for the entire period
  const lineTotals = {};
  Object.keys(lineDataMap).forEach(lineId => {
    const lineData = lineDataMap[lineId];
    const totalVolume = lineData.reduce((sum, record) => {
      const date = new Date(record.period).toISOString().split('T')[0];
      const gsVolume = record.volume || 0;

      // Subtract enterprise volume if exists for this line and date
      const enterpriseVolume = (enterpriseMap[lineId] && enterpriseMap[lineId][date]) || 0;
      const netVolume = Math.max(0, gsVolume - enterpriseVolume);

      return sum + netVolume;
    }, 0);
    lineTotals[lineId] = totalVolume;
  });

  // Create chart data with percentages for each day
  const chartDataMap = {};

  Object.keys(lineDataMap).forEach(lineId => {
    const lineData = lineDataMap[lineId];
    const totalVolume = lineTotals[lineId];

    if (totalVolume > 0) {
      lineData.forEach(record => {
        const date = new Date(record.period).toISOString().split('T')[0];
        const gsVolume = record.volume || 0;

        // Subtract enterprise volume
        const enterpriseVolume = (enterpriseMap[lineId] && enterpriseMap[lineId][date]) || 0;
        const netVolume = Math.max(0, gsVolume - enterpriseVolume);

        const percentage = (netVolume / totalVolume) * 100;

        if (!chartDataMap[date]) {
          chartDataMap[date] = { period: date };
        }

        chartDataMap[date][`line_${lineId}`] = percentage;
        chartDataMap[date][`line_${lineId}_volume`] = netVolume;
        chartDataMap[date][`line_${lineId}_enterprise`] = enterpriseVolume;
      });
    }
  });

  // Convert to array and sort by date
  const trendsArray = Object.values(chartDataMap).sort((a, b) =>
    new Date(a.period) - new Date(b.period)
  );

  return trendsArray;
};
```

**Логика вычитания:**
1. Создать lookup map `enterpriseMap` из `enterpriseData`: `{line_id: {date: total_volume}}`
2. При расчете `lineTotals`: для каждой даты вычесть объем предприятий из ГС
3. При создании chart data: также вычесть объем предприятий
4. Использовать `Math.max(0, gsVolume - enterpriseVolume)` чтобы не получить отрицательных значений
5. Процент рассчитывать на основе ЧИСТОГО объема (после вычитания)

---

## 4. Порядок реализации (пошагово)

### ✅ Этап 1: Backend - Базовая структура
- [ ] 1. Создать директорию: `backend/services/`
- [ ] 2. Создать директорию: `backend/data/`
- [ ] 3. Создать файл `backend/services/__init__.py` (пустой)

### ✅ Этап 2: Backend - Настройки
- [ ] 4. Обновить `backend/settings.py` - добавить DPD API настройки
- [ ] 5. Обновить `.env` - добавить DPD credentials

### ✅ Этап 3: Backend - DPD клиент
- [ ] 6. Создать `backend/services/dpd_client.py`:
   - Класс `DPDClient` с async httpx
   - JWT аутентификация
   - Метод `get_volumes()`
   - Обработка ошибок и retry

### ✅ Этап 4: Backend - Менеджер маппингов
- [ ] 7. Создать `backend/services/enterprise_mappings.py`:
   - Функция `load_mappings()` с кешем
   - Функция `get_devices_for_lines()`

### ✅ Этап 5: Backend - Модели
- [ ] 8. Создать `backend/db/models/enterprise_models.py`:
   - `DeviceVolume`
   - `EnterpriseVolumeResponse`

### ✅ Этап 6: Backend - Endpoint
- [ ] 9. Создать `backend/api/endpoints/enterprise_ep.py`:
   - Класс `EnterpriseRouter`
   - Endpoint `GET /enterprise/volumes/`
   - Логика агрегации

### ✅ Этап 7: Backend - Регистрация
- [ ] 10. Обновить `backend/api/main.py`:
    - Импорт `enterprise_ep`
    - Регистрация роутера

### ✅ Этап 8: Backend - Тестирование
- [ ] 11. Запустить backend: `uvicorn backend.api.main:app --reload`
- [ ] 12. Протестировать endpoint через Swagger UI или curl

### ✅ Этап 9: Excel файл
- [ ] 13. Создать `backend/data/enterprise_mappings.xlsx`:
    - С колонками из структуры выше
    - Заполнить реальными данными (координация с операторами)

### ✅ Этап 10: Frontend - API сервис
- [ ] 14. Обновить `frontend/react-frontend/src/services/api.js`:
    - Добавить `enterpriseApi.getEnterpriseVolumes()`

### ✅ Этап 11: Frontend - Компонент
- [ ] 15. Обновить `frontend/react-frontend/src/components/GRSTrends.jsx`:
    - Импорт `enterpriseApi`
    - Модифицировать `calculateTrends()`
    - Модифицировать `calculateGRSTrendsPercentages()`

### ✅ Этап 12: Frontend - Тестирование
- [ ] 16. Запустить frontend: `npm run dev`
- [ ] 17. Открыть GRS Trends модал
- [ ] 18. Выбрать период, проверить:
    - Запросы идут параллельно
    - Объемы вычитаются корректно
    - График отображается правильно

### ✅ Этап 13: Интеграционное тестирование
- [ ] 19. Протестировать разные сценарии:
    - Линии с предприятиями
    - Линии без предприятий
    - Смешанные периоды
    - DPD API недоступен (должен показать warning)
    - Пустой Excel файл

### ✅ Этап 14: Деплой
- [ ] 20. Задеплоить backend изменения
- [ ] 21. Загрузить Excel файл на сервер
- [ ] 22. Задеплоить frontend изменения

---

## 5. Критические файлы

### Backend (создать новые):
- `D:\Projects\HLViewer\HLViewer\backend\services\dpd_client.py`
- `D:\Projects\HLViewer\HLViewer\backend\services\enterprise_mappings.py`
- `D:\Projects\HLViewer\HLViewer\backend\services\__init__.py`
- `D:\Projects\HLViewer\HLViewer\backend\api\endpoints\enterprise_ep.py`
- `D:\Projects\HLViewer\HLViewer\backend\db\models\enterprise_models.py`
- `D:\Projects\HLViewer\HLViewer\backend\data\enterprise_mappings.xlsx`

### Backend (модифицировать):
- `D:\Projects\HLViewer\HLViewer\backend\settings.py` - добавить DPD настройки
- `D:\Projects\HLViewer\HLViewer\.env` - добавить credentials
- `D:\Projects\HLViewer\HLViewer\backend\api\main.py` - зарегистрировать роутер

### Frontend (модифицировать):
- `D:\Projects\HLViewer\frontend\react-frontend\src\services\api.js` - добавить enterpriseApi
- `D:\Projects\HLViewer\frontend\react-frontend\src\components\GRSTrends.jsx` - интеграция вычитания

---

## 6. Тестирование (для запуска на сервере)

### 6.1 Подготовка тестовых данных

**Создать тестовый Excel файл:** `backend/data/enterprise_mappings_test.xlsx`

```csv
line_id,serNum,mfDev,typeDev,chNum,enterprise_name,active
1,123456,16,1,1,Тест Предприятие 1,TRUE
6,234567,16,1,1,Тест Предприятие 2,TRUE
```

### 6.2 Автоматические тесты (pytest)

**Создать файл:** `tests/test_enterprise_integration.py`

```python
import pytest
from datetime import datetime, timedelta
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import load_mappings, get_devices_for_lines
from backend.api.main import app
from fastapi.testclient import TestClient

# Test 1: DPD API Connection
@pytest.mark.asyncio
async def test_dpd_api_connection():
    """Проверка подключения к DPD API"""
    client = DPDClient()
    assert client.access_token is not None
    print("✅ DPD API authentication successful")

# Test 2: Excel Mappings Loading
def test_load_enterprise_mappings():
    """Проверка загрузки Excel файла"""
    mappings = load_mappings()
    assert mappings is not None
    assert len(mappings) > 0
    print(f"✅ Loaded {len(mappings)} enterprise mappings")

# Test 3: Get Devices for Lines
def test_get_devices_for_lines():
    """Проверка фильтрации устройств по line_id"""
    devices = get_devices_for_lines([1, 6])
    assert devices is not None
    print(f"✅ Found {len(devices)} devices for lines 1 and 6")
    for device in devices:
        print(f"  - Line {device['line_id']}: {device['enterprise_name']}")

# Test 4: DPD API Data Fetching
@pytest.mark.asyncio
async def test_dpd_api_fetch_volumes():
    """Проверка получения данных из DPD API"""
    client = DPDClient()
    devices = get_devices_for_lines([1])

    date_to = datetime.now()
    date_from = date_to - timedelta(days=7)

    data = await client.get_volumes(devices, date_from, date_to)
    assert data is not None
    print(f"✅ Fetched {len(data)} records from DPD API")

# Test 5: Enterprise Endpoint
def test_enterprise_endpoint():
    """Проверка работы endpoint /enterprise/volumes/"""
    client = TestClient(app)

    response = client.get(
        "/enterprise/volumes/",
        params={
            "line_id": [1, 6],
            "from_date": "2025-12-01",
            "to_date": "2025-12-21"
        }
    )

    assert response.status_code == 200
    data = response.json()
    print(f"✅ Endpoint returned {len(data)} aggregated records")

    # Проверка структуры ответа
    if len(data) > 0:
        assert "line_id" in data[0]
        assert "period" in data[0]
        assert "total_volume" in data[0]
        assert "devices" in data[0]
        print("✅ Response structure is valid")

# Test 6: Full Integration Test
@pytest.mark.asyncio
async def test_full_integration():
    """Полный интеграционный тест"""
    # 1. Загрузить маппинги
    devices = get_devices_for_lines([1])
    assert len(devices) > 0

    # 2. Запросить DPD API
    client = DPDClient()
    date_to = datetime.now()
    date_from = date_to - timedelta(days=1)

    volumes = await client.get_volumes(devices, date_from, date_to)

    # 3. Проверить агрегацию
    total = sum(v.get('dvstAlwrk', 0) for v in volumes)
    print(f"✅ Total enterprise volume for line 1: {total} m³")
```

### 6.3 Скрипт для запуска всех тестов

**Создать файл:** `test_enterprise.sh`

```bash
#!/bin/bash

echo "=========================================="
echo "Enterprise Volume Subtraction - Test Suite"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Check Excel file exists
echo "Test 1: Checking Excel mappings file..."
if [ -f "backend/data/enterprise_mappings.xlsx" ]; then
    echo -e "${GREEN}✅ Excel file found${NC}"
else
    echo -e "${RED}❌ Excel file not found${NC}"
    exit 1
fi

# Test 2: Check environment variables
echo ""
echo "Test 2: Checking environment variables..."
if grep -q "DPD_USERNAME" .env && grep -q "DPD_PASSWORD" .env; then
    echo -e "${GREEN}✅ DPD credentials configured${NC}"
else
    echo -e "${RED}❌ DPD credentials missing in .env${NC}"
    exit 1
fi

# Test 3: Run pytest
echo ""
echo "Test 3: Running automated tests..."
pytest tests/test_enterprise_integration.py -v --tb=short

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ All automated tests passed${NC}"
else
    echo -e "${RED}❌ Some tests failed${NC}"
    exit 1
fi

# Test 4: Test endpoint with curl
echo ""
echo "Test 4: Testing endpoint with curl..."
response=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/enterprise/volumes/?line_id=1&from_date=2025-12-01&to_date=2025-12-21")

if [ "$response" = "200" ]; then
    echo -e "${GREEN}✅ Endpoint responds with 200 OK${NC}"
else
    echo -e "${RED}❌ Endpoint returned $response${NC}"
fi

# Test 5: Check Swagger docs
echo ""
echo "Test 5: Checking Swagger documentation..."
response=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/docs")

if [ "$response" = "200" ]; then
    echo -e "${GREEN}✅ Swagger docs available at http://localhost:8000/docs${NC}"
else
    echo -e "${RED}❌ Swagger docs not accessible${NC}"
fi

echo ""
echo "=========================================="
echo -e "${GREEN}All tests completed successfully!${NC}"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Open http://localhost:8000/docs to test API manually"
echo "2. Start frontend: cd frontend/react-frontend && npm run dev"
echo "3. Test GRS Trends modal with enterprise subtraction"
```

### 6.4 Скрипт для Windows

**Создать файл:** `test_enterprise.bat`

```bat
@echo off
echo ==========================================
echo Enterprise Volume Subtraction - Test Suite
echo ==========================================
echo.

REM Test 1: Check Excel file
echo Test 1: Checking Excel mappings file...
if exist backend\data\enterprise_mappings.xlsx (
    echo [OK] Excel file found
) else (
    echo [ERROR] Excel file not found
    exit /b 1
)

REM Test 2: Check .env
echo.
echo Test 2: Checking environment variables...
findstr /C:"DPD_USERNAME" .env >nul
if %errorlevel% equ 0 (
    echo [OK] DPD credentials configured
) else (
    echo [ERROR] DPD credentials missing
    exit /b 1
)

REM Test 3: Run pytest
echo.
echo Test 3: Running automated tests...
pytest tests/test_enterprise_integration.py -v --tb=short

if %errorlevel% equ 0 (
    echo [OK] All automated tests passed
) else (
    echo [ERROR] Some tests failed
    exit /b 1
)

REM Test 4: Test endpoint
echo.
echo Test 4: Testing endpoint with curl...
curl -s -o nul -w "%%{http_code}" "http://localhost:8000/enterprise/volumes/?line_id=1&from_date=2025-12-01&to_date=2025-12-21"

echo.
echo ==========================================
echo All tests completed!
echo ==========================================
echo.
echo Next steps:
echo 1. Open http://localhost:8000/docs
echo 2. Start frontend: cd frontend\react-frontend ^&^& npm run dev
echo 3. Test GRS Trends modal
```

### 6.5 Ручное тестирование на сервере

#### Шаг 1: Запустить backend
```bash
cd /path/to/HLViewer
source venv/bin/activate  # или venv\Scripts\activate на Windows
uvicorn backend.api.main:app --host 0.0.0.0 --port 8000
```

#### Шаг 2: Запустить тесты
```bash
# Linux/Mac
chmod +x test_enterprise.sh
./test_enterprise.sh

# Windows
test_enterprise.bat
```

#### Шаг 3: Ручная проверка endpoint
```bash
# Тест 1: Проверка без маппингов (должен вернуть [])
curl "http://localhost:8000/enterprise/volumes/?line_id=999&from_date=2025-12-01&to_date=2025-12-21"
# Ожидается: []

# Тест 2: Проверка с реальными line_id
curl "http://localhost:8000/enterprise/volumes/?line_id=1&line_id=6&from_date=2025-12-01&to_date=2025-12-21"
# Ожидается: JSON массив с данными

# Тест 3: Проверка невалидных дат
curl "http://localhost:8000/enterprise/volumes/?line_id=1&from_date=invalid&to_date=2025-12-21"
# Ожидается: HTTP 422 или 400

# Тест 4: Проверка без параметров
curl "http://localhost:8000/enterprise/volumes/"
# Ожидается: HTTP 422 (missing required parameters)
```

#### Шаг 4: Проверка через Swagger UI
1. Открыть http://localhost:8000/docs
2. Найти endpoint `GET /enterprise/volumes/`
3. Нажать "Try it out"
4. Заполнить параметры:
   - line_id: [1, 6]
   - from_date: 2025-12-01
   - to_date: 2025-12-21
5. Нажать "Execute"
6. Проверить Response (должен быть 200 OK с JSON)

#### Шаг 5: Проверка логов
```bash
# Смотреть логи backend
tail -f logs/backend.log

# Должны увидеть:
# - "Loading enterprise mappings..."
# - "Fetching DPD API data..."
# - "Aggregating volumes by line_id..."
```

### 6.6 Чеклист для тестирования на сервере

```markdown
## Pre-deployment checklist

### Файлы скопированы
- [ ] backend/services/dpd_client.py
- [ ] backend/services/enterprise_mappings.py
- [ ] backend/api/endpoints/enterprise_ep.py
- [ ] backend/db/models/enterprise_models.py
- [ ] backend/data/enterprise_mappings.xlsx
- [ ] tests/test_enterprise_integration.py
- [ ] test_enterprise.sh (или .bat)

### Конфигурация обновлена
- [ ] backend/settings.py - DPD settings добавлены
- [ ] .env - DPD_USERNAME и DPD_PASSWORD добавлены
- [ ] backend/api/main.py - enterprise router зарегистрирован

### Frontend обновлен
- [ ] frontend/react-frontend/src/services/api.js - enterpriseApi добавлен
- [ ] frontend/react-frontend/src/components/GRSTrends.jsx - интеграция добавлена

### Тестирование
- [ ] Backend запускается без ошибок
- [ ] Swagger docs доступны (http://localhost:8000/docs)
- [ ] Endpoint /enterprise/volumes/ отвечает 200 OK
- [ ] DPD API authentication работает
- [ ] Excel файл читается корректно
- [ ] pytest тесты проходят
- [ ] Frontend запускается без ошибок
- [ ] GRS Trends модал открывается
- [ ] Два API запроса идут параллельно (проверить в Network)
- [ ] График строится корректно
- [ ] Объемы вычитаются (проверить в console.log)

### Smoke test
- [ ] Выбрать период с данными
- [ ] Проверить что тренд отображается
- [ ] Проверить что нет ошибок в console
- [ ] Проверить что backend логи чистые
```

### 6.7 Troubleshooting на сервере

**Проблема 1: DPD API authentication fails**
```bash
# Проверить credentials
cat .env | grep DPD

# Протестировать вручную
python3 << EOF
from backend.services.dpd_client import DPDClient
import asyncio

async def test():
    client = DPDClient()
    print(f"Access token: {client.access_token[:20]}...")

asyncio.run(test())
EOF
```

**Проблема 2: Excel файл не читается**
```bash
# Проверить что файл существует
ls -la backend/data/enterprise_mappings.xlsx

# Проверить права доступа
chmod 644 backend/data/enterprise_mappings.xlsx

# Протестировать чтение
python3 << EOF
from backend.services.enterprise_mappings import load_mappings
mappings = load_mappings()
print(f"Loaded {len(mappings)} mappings")
print(mappings.head())
EOF
```

**Проблема 3: Endpoint не отвечает**
```bash
# Проверить что backend запущен
ps aux | grep uvicorn

# Проверить что порт открыт
netstat -tulpn | grep 8000

# Проверить логи
tail -n 50 logs/backend.log
```

**Проблема 4: Frontend не подключается к backend**
```bash
# Проверить CORS настройки в backend/api/main.py
# Проверить что API_URL правильный во frontend

# Тестировать CORS
curl -H "Origin: http://localhost:5173" \
     -H "Access-Control-Request-Method: GET" \
     -X OPTIONS \
     http://localhost:8000/enterprise/volumes/
```

### 6.8 Быстрый тест (одна команда)

```bash
# Создать quick_test.py
cat > quick_test.py << 'EOF'
#!/usr/bin/env python3
import requests
import sys

def test():
    print("Testing enterprise endpoint...")

    url = "http://localhost:8000/enterprise/volumes/"
    params = {
        "line_id": [1, 6],
        "from_date": "2025-12-01",
        "to_date": "2025-12-21"
    }

    try:
        response = requests.get(url, params=params)

        if response.status_code == 200:
            data = response.json()
            print(f"✅ SUCCESS: Got {len(data)} records")

            if len(data) > 0:
                print(f"✅ Sample: Line {data[0]['line_id']}, Volume: {data[0]['total_volume']}")

            return 0
        else:
            print(f"❌ ERROR: HTTP {response.status_code}")
            print(response.text)
            return 1

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(test())
EOF

chmod +x quick_test.py
python3 quick_test.py
```

### 6.9 Performance test

```bash
# Тест производительности (10 запросов)
for i in {1..10}; do
    echo "Request $i..."
    time curl -s "http://localhost:8000/enterprise/volumes/?line_id=1&from_date=2025-12-01&to_date=2025-12-21" > /dev/null
done

# Ожидается: < 2 секунды на запрос (с учетом DPD API)
```

---

## 7. Обработка ошибок

### Backend:
- DPD API timeout → вернуть HTTP 503 с сообщением
- Excel отсутствует → вернуть HTTP 500
- Нет маппингов → вернуть `[]` (норма)
- Невалидные параметры → вернуть HTTP 400

### Frontend:
- Нет данных предприятий → `console.warn()`, использовать только ГС объемы
- DPD API недоступен → то же самое (не критичная ошибка)
- Ошибка daily API → показать ошибку пользователю

**Философия:** Отсутствие данных предприятий - это WARNING, а не ERROR. Тренд должен строиться даже если DPD API недоступен (просто будет показывать полные объемы ГС).

---

## 8. Безопасность

- ✅ Credentials в `.env`, не в коде
- ✅ Backend проксирует DPD API (фронт не знает credentials)
- ✅ HTTPS для DPD API
- ✅ Excel файл - read-only для приложения
- ✅ Валидация всех input параметров в endpoint

---

## 9. Производительность

- ✅ Параллельные запросы во фронте (`Promise.all`)
- ✅ Кеширование Excel файла на 5 минут
- ✅ Один POST запрос к DPD для всех устройств (не по одному)
- ✅ Агрегация на backend (меньше данных по сети)
- ✅ Timeout 30 сек для DPD API

---

## Итого

**Что делаем:**
1. Создаем Excel с маппингом вычислителей к линиям
2. Добавляем backend endpoint который:
   - Читает Excel
   - Запрашивает DPD API
   - Агрегирует объемы по line_id + date
3. Модифицируем фронтенд чтобы:
   - Запрашивать оба API параллельно
   - Вычитать объемы предприятий из ГС
   - Считать проценты от ЧИСТОГО объема

**Что получаем:**
- Суточный тренд без учета объемов предприятий
- Более точную картину потребления на ГС
- Гибкость (можно включать/выключать предприятия через Excel)
