# Тесты HLViewer Backend

## Интеграционный тест enterprise mappings

Файл: `test_enterprise_integration.py`

### Описание

Этот тест валидирует корректность работы enterprise mappings путем:

1. **Test 1: Direct DPD API Access**
   - Загружает 50 устройств из `enterprise.xlsx` и `line_id.xlsx`
   - Запрашивает данные напрямую из DPD API
   - Проверяет что API возвращает данные для наших устройств

2. **Test 2: Manual Aggregation**
   - Агрегирует данные DPD вручную по `line_id` и дате
   - Проверяет структуру данных
   - Выводит статистику по линиям

3. **Test 3: Compare Manual vs API Aggregation**
   - Сравнивает ручную агрегацию с логикой API endpoint
   - Проверяет что суммарные объемы совпадают
   - Проверяет что все записи идентичны

### Конфигурация

В начале файла можно настроить параметры теста:

```python
TEST_DEVICE_COUNT = 50  # Количество устройств для теста
TEST_DATE_FROM = datetime.now() - timedelta(days=7)  # Начало периода
TEST_DATE_TO = datetime.now() - timedelta(days=1)  # Конец периода
TEST_PERIOD_TYPE = "daily"  # "daily" или "hourly"
```

### Запуск

#### Из рабочей сети (с доступом к DPD API):

```bash
# Запуск всех тестов
cd D:\Projects\HLViewer\HLViewer
python -m pytest backend/tests/test_enterprise_integration.py -v -s

# Запуск конкретного теста
python -m pytest backend/tests/test_enterprise_integration.py::TestEnterpriseIntegration::test_dpd_api_direct_access -v -s

# С детальным выводом
python -m pytest backend/tests/test_enterprise_integration.py -v -s --tb=short
```

#### Запуск напрямую:

```bash
cd D:\Projects\HLViewer\HLViewer
python backend/tests/test_enterprise_integration.py
```

### Требования

- ✅ Файлы `backend/data/enterprise.xlsx` и `backend/data/line_id.xlsx` должны существовать
- ✅ Доступ к DPD API (корпоративная сеть)
- ✅ Настроенные переменные окружения для DPD API в `.env` или `backend/settings.py`:
  - `DPD_API_BASE_URL`
  - `DPD_AUTH_URL`
  - `DPD_USERNAME`
  - `DPD_PASSWORD`

### Ожидаемые результаты

При успешном прохождении тестов вы увидите:

```
================================================================================
TEST 1: Direct DPD API Access
================================================================================

DPD API Results:
  Total records: 350
  Devices with data: 48 / 50

  Sample record:
    Device: serNum=7942, mfDev=1, typeDev=5, chNum=0
    Date: 2025-12-20
    Volume (dvstAlwrk): 1234.56

================================================================================
TEST 2: Manual Aggregation
================================================================================

Aggregation Results:
  Unique (line_id, date) combinations: 245
  Records per line_id:
    line_id 1: 49 time periods
    line_id 8: 42 time periods
    ...

================================================================================
TEST 3: Compare Manual vs API Aggregation
================================================================================

Manual Aggregation:
  Records: 245
  Total volume (all periods): 12345.67

API-style Aggregation:
  Records: 245
  Total volume (all periods): 12345.67

Comparison:
  ✓ Record count matches: 245
  ✓ Total volume matches: 12345.67
  ✓ All individual records match

================================================================================
✓ ALL TESTS PASSED
================================================================================
```

### Устранение ошибок

#### ConnectError: [Errno 11001] getaddrinfo failed

Тест запущен вне корпоративной сети. Запустите тест с компьютера с доступом к DPD API.

#### FileNotFoundError: Enterprise file not found

Убедитесь что файлы `enterprise.xlsx` и `line_id.xlsx` существуют в `backend/data/`.

#### Missing required columns

Проверьте структуру файлов - они должны иметь правильные колонки (см. документацию в `backend/data/README_ENTERPRISE_MAPPINGS.md`).

## Добавление новых тестов

Создавайте новые файлы тестов в формате `test_*.py` в этой папке. Pytest автоматически обнаружит их.

Пример:

```python
import pytest

class TestMyFeature:
    def test_something(self):
        assert True
```
