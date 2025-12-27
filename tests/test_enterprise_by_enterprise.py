"""
Тест для исследования структуры данных по предприятиям.

Выводит объемы газа по суткам для всех предприятий линии 4.
"""

import pytest
import httpx
from datetime import datetime
from collections import defaultdict


API_BASE_URL = "http://localhost:8000"
TEST_LINE_ID = 4
TEST_DATE_FROM = "2025-12-01"
TEST_DATE_TO = "2025-12-25"


def test_volumes_by_enterprise_line_4():
    """
    Тест: Вывод объемов по предприятиям для линии 4.

    Группирует данные по предприятиям и выводит объемы по дням.
    """
    client = httpx.Client(
        base_url=API_BASE_URL,
        timeout=30.0,
        verify=False,
        trust_env=False
    )

    # Запрос данных
    params = {
        "line_id": [TEST_LINE_ID],
        "from_date": TEST_DATE_FROM,
        "to_date": TEST_DATE_TO
    }
    response = client.get("/enterprise/volumes/", params=params)

    assert response.status_code == 200, f"Ошибка API: {response.status_code}"

    data = response.json()
    print(f"\n{'='*80}")
    print(f"Объемы по предприятиям для линии {TEST_LINE_ID}")
    print(f"Период: {TEST_DATE_FROM} - {TEST_DATE_TO}")
    print(f"{'='*80}\n")

    # Группировка по (enterprise_name, date)
    by_enterprise = defaultdict(lambda: defaultdict(float))
    enterprise_devices = defaultdict(set)  # для подсчета устройств

    for record in data:
        period = record["period"]

        for device in record["devices"]:
            enterprise = device["enterprise_name"]
            volume = device["volume"]
            device_key = (device["serNum"], device["mfDev"], device["typeDev"], device["chNum"])

            by_enterprise[enterprise][period] += volume
            enterprise_devices[enterprise].add(device_key)

    # Вывод результатов
    for enterprise in sorted(by_enterprise.keys()):
        print(f"\n📊 {enterprise}")
        print(f"   Количество устройств: {len(enterprise_devices[enterprise])}")
        print(f"   {'─'*70}")

        # Сортируем даты
        periods = sorted(by_enterprise[enterprise].keys())

        # Вывод по дням
        total_enterprise = 0.0
        for period in periods:
            volume = by_enterprise[enterprise][period]
            total_enterprise += volume
            print(f"   {period}: {volume:>10.2f} м³")

        print(f"   {'─'*70}")
        print(f"   ИТОГО: {total_enterprise:>10.2f} м³")

    # Общая статистика
    print(f"\n{'='*80}")
    print(f"ОБЩАЯ СТАТИСТИКА:")
    print(f"  Предприятий: {len(by_enterprise)}")
    print(f"  Дней с данными: {len(data)}")

    grand_total = sum(record["total_volume"] for record in data)
    print(f"  Общий объем за период: {grand_total:.2f} м³")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
