"""
Тест виртуальных линий для августа 2025 года.
"""

import httpx
import json
from datetime import datetime

API_BASE_URL = "http://localhost:8000"

def print_section(title):
    """Печать заголовка секции."""
    print(f"\n{'='*80}")
    print(f"{title}")
    print(f"{'='*80}\n")


def test_validate_config():
    """Тест 1: Валидация конфигурации виртуальных линий."""
    print_section("ТЕСТ 1: Валидация конфигурации /virtual_lines/validate")

    with httpx.Client(base_url=API_BASE_URL, timeout=10.0, verify=False, trust_env=False) as client:
        response = client.get("/virtual_lines/validate")

        print(f"Статус: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"\nРезультаты валидации:")
            print(f"  [OK] Валидна: {data['valid']}")
            print(f"  [OK] Всего виртуальных линий: {data['virtual_lines_count']}")
            print(f"  [OK] Активных виртуальных линий: {data['active_count']}")
            print(f"  [OK] Скрытых физических линий: {data['hidden_physical_lines']}")

            if data['errors']:
                print(f"\n  [!] Ошибки:")
                for error in data['errors']:
                    print(f"    - {error}")

            if data['warnings']:
                print(f"\n  [!] Предупреждения:")
                for warning in data['warnings']:
                    print(f"    - {warning}")
        else:
            print(f"Ошибка: {response.text}")


def test_visible_lines():
    """Тест 2: Получение списка видимых линий."""
    print_section("ТЕСТ 2: Список видимых линий /virtual_lines/visible")

    with httpx.Client(base_url=API_BASE_URL, timeout=10.0, verify=False, trust_env=False) as client:
        response = client.get("/virtual_lines/visible")

        print(f"Статус: {response.status_code}")

        if response.status_code == 200:
            data = response.json()

            virtual_lines = [line for line in data if line['is_virtual']]
            physical_lines = [line for line in data if not line['is_virtual']]

            print(f"\nВсего видимых линий: {len(data)}")
            print(f"  - Виртуальных: {len(virtual_lines)}")
            print(f"  - Физических (не в кольцах): {len(physical_lines)}")

            print("\nВиртуальные линии:")
            for line in sorted(virtual_lines, key=lambda x: x['id']):
                print(f"  {line['id']}: {line['name']}")
                print(f"      Физические линии: {line['physical_line_ids']}")
                if line['description']:
                    print(f"      Описание: {line['description']}")

            if physical_lines:
                print(f"\nФизические линии (не в кольцах): {len(physical_lines)}")
                for line in sorted(physical_lines, key=lambda x: x['id'])[:10]:  # Первые 10
                    print(f"  {line['id']}: {line['name']}")
                if len(physical_lines) > 10:
                    print(f"  ... и еще {len(physical_lines) - 10} линий")
        else:
            print(f"Ошибка: {response.text}")


def test_hourly_virtual_august():
    """Тест 3: Часовые архивы для виртуальных линий (август 2025)."""
    print_section("ТЕСТ 3: Часовые архивы /hourly_virtual/ (август 2025)")

    # Тестируем первую неделю августа
    from_date = "2025-08-01T00:00:00"
    to_date = "2025-08-07T23:59:59"

    # Тестируем виртуальные линии 1001 и 1002
    virtual_line_ids = [1001, 1002]

    print(f"Период: {from_date} - {to_date}")
    print(f"Виртуальные линии: {virtual_line_ids}")

    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, verify=False, trust_env=False) as client:
        params = {
            "from_date": from_date,
            "to_date": to_date,
            "line_id": virtual_line_ids
        }

        response = client.get("/hourly_virtual/", params=params)

        print(f"\nСтатус: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"Получено записей: {len(data)}")

            # Группировка по линиям
            by_line = {}
            for record in data:
                line_id = record['line_id']
                if line_id not in by_line:
                    by_line[line_id] = []
                by_line[line_id].append(record)

            print(f"\nДанные по линиям:")
            for line_id in sorted(by_line.keys()):
                records = by_line[line_id]
                total_volume = sum(r['volume'] for r in records)
                avg_pressure = sum(r['pressure'] for r in records) / len(records)

                print(f"\n  Виртуальная линия {line_id}:")
                print(f"    Записей: {len(records)}")
                print(f"    Общий объем: {total_volume:,.2f} куб.м")
                print(f"    Средне давление: {avg_pressure:.2f} кгс/см2")

                # Показать несколько первых записей
                print(f"    Примеры записей:")
                for record in records[:3]:
                    print(f"      {record['period']}: {record['volume']:.2f} куб.м, P={record['pressure']:.2f}")
        else:
            print(f"Ошибка: {response.text}")


def test_daily_virtual_august():
    """Тест 4: Суточные архивы для виртуальных линий (август 2025)."""
    print_section("ТЕСТ 4: Суточные архивы /daily_virtual/ (весь август 2025)")

    from_date = "2025-08-01"
    to_date = "2025-08-31"

    # Тестируем все виртуальные линии
    virtual_line_ids = [1001, 1002, 1003, 1004]

    print(f"Период: {from_date} - {to_date}")
    print(f"Виртуальные линии: {virtual_line_ids}")

    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, verify=False, trust_env=False) as client:
        params = {
            "from_date": from_date,
            "to_date": to_date,
            "line_id": virtual_line_ids
        }

        response = client.get("/daily_virtual/", params=params)

        print(f"\nСтатус: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"Получено записей: {len(data)}")

            # Группировка по линиям
            by_line = {}
            for record in data:
                line_id = record['line_id']
                if line_id not in by_line:
                    by_line[line_id] = []
                by_line[line_id].append(record)

            print(f"\nДанные по виртуальным линиям за август 2025:")
            print(f"{'-'*80}")

            for line_id in sorted(by_line.keys()):
                records = by_line[line_id]
                total_volume = sum(r['volume'] for r in records)
                avg_pressure = sum(r['pressure'] for r in records) / len(records) if records else 0
                avg_temp = sum(r['temperature'] for r in records) / len(records) if records else 0

                print(f"\n  Виртуальная линия {line_id}:")
                print(f"     Дней с данными: {len(records)}")
                print(f"     Общий объем за август: {total_volume:,.2f} куб.м")
                print(f"     Средне давление: {avg_pressure:.2f} кгс/см2")
                print(f"     Средняя температура: {avg_temp:.2f} C")

                # Показать первые 5 дней
                print(f"     Первые 5 дней:")
                for record in sorted(records, key=lambda x: x['period'])[:5]:
                    print(f"       {record['period']}: {record['volume']:>10.2f} куб.м, P={record['pressure']:.2f}, T={record['temperature']:.2f}")
        else:
            print(f"Ошибка: {response.text}")


def test_mixed_virtual_physical():
    """Тест 5: Смешанный запрос (виртуальные + физические линии)."""
    print_section("ТЕСТ 5: Смешанный запрос - виртуальные + физические линии")

    from_date = "2025-08-01"
    to_date = "2025-08-07"

    # Запросим виртуальную линию 1001 и физическую линию 25
    line_ids = [1001, 25]

    print(f"Период: {from_date} - {to_date}")
    print(f"Линии: {line_ids} (1001 - виртуальная, 25 - физическая)")

    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, verify=False, trust_env=False) as client:
        params = {
            "from_date": from_date,
            "to_date": to_date,
            "line_id": line_ids
        }

        response = client.get("/daily_virtual/", params=params)

        print(f"\nСтатус: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"Получено записей: {len(data)}")

            # Разделить на виртуальные и физические
            virtual_records = [r for r in data if r['line_id'] >= 1000]
            physical_records = [r for r in data if r['line_id'] < 1000]

            print(f"\nВиртуальные линии: {len(virtual_records)} записей")
            print(f"Физические линии: {len(physical_records)} записей")

            if virtual_records:
                total = sum(r['volume'] for r in virtual_records)
                print(f"\nВиртуальная линия 1001:")
                print(f"  Объем за неделю: {total:,.2f} куб.м")

            if physical_records:
                total = sum(r['volume'] for r in physical_records)
                print(f"\nФизическая линия 25:")
                print(f"  Объем за неделю: {total:,.2f} куб.м")
        else:
            print(f"Ошибка: {response.text}")


if __name__ == "__main__":
    print("\n" + "="*80)
    print("ТЕСТИРОВАНИЕ ВИРТУАЛЬНЫХ ЛИНИЙ - АВГУСТ 2025")
    print("="*80)

    try:
        # Проверка доступности API
        with httpx.Client(base_url=API_BASE_URL, timeout=5.0, verify=False, trust_env=False) as client:
            response = client.get("/docs")
            if response.status_code != 200:
                print(f"\n[!] API недоступен по адресу {API_BASE_URL}")
                print("Запустите сервер: uvicorn backend.api.main:app --reload")
                exit(1)

        # Запуск тестов
        test_validate_config()
        test_visible_lines()
        test_hourly_virtual_august()
        test_daily_virtual_august()
        test_mixed_virtual_physical()

        print_section("[OK] ВСЕ ТЕСТЫ ЗАВЕРШЕНЫ")

    except httpx.ConnectError:
        print(f"\n[ERROR] Не удалось подключиться к API по адресу {API_BASE_URL}")
        print("Убедитесь, что сервер запущен: uvicorn backend.api.main:app --reload")
    except Exception as e:
        print(f"\n[ERROR] Ошибка при выполнении тестов: {e}")
        import traceback
        traceback.print_exc()
