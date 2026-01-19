"""
Тест для проверки опроса предприятий (Enterprise Polling API)

Этот тест проверяет:
1. Получение списка всех предприятий
2. Опрос данных для конкретных предприятий
3. Проверку полей давления и температуры (press/temper)
"""

import asyncio
import httpx
from datetime import datetime, timedelta
from typing import List, Dict


# Конфигурация
BASE_URL = "http://10.130.8.141:8000"  # Измените на ваш URL бекенда
TEST_ENTERPRISES_COUNT = 20  # Количество предприятий для тестирования


async def get_all_enterprises() -> List[Dict]:
    """Получить список всех предприятий"""
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        response = await client.get(f"{BASE_URL}/enterprise/mappings/")
        response.raise_for_status()
        return response.json()


async def poll_enterprise(line_id: int, ser_num: int, ch_num: int,
                         from_date: str, to_date: str, period_type: str = "daily") -> List[Dict]:
    """Опросить конкретное предприятие"""
    params = {
        "line_id": line_id,
        "serNum": ser_num,
        "chNum": ch_num,
        "from_date": from_date,
        "to_date": to_date,
        "period_type": period_type
    }

    async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
        response = await client.get(f"{BASE_URL}/enterprise/volumes/", params=params)
        response.raise_for_status()
        return response.json()


def print_separator(title: str = ""):
    """Печать разделителя"""
    print("\n" + "=" * 80)
    if title:
        print(f"  {title}")
        print("=" * 80)
    else:
        print("=" * 80)


async def test_enterprise_polling():
    """Основной тест опроса предприятий"""

    print_separator("ТЕСТ ОПРОСА ПРЕДПРИЯТИЙ")
    print(f"Дата теста: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Получаем список предприятий
    print_separator("Шаг 1: Получение списка предприятий")
    try:
        enterprises = await get_all_enterprises()
        print(f"✓ Получено предприятий: {len(enterprises)}")

        if not enterprises:
            print("✗ ОШИБКА: Список предприятий пуст!")
            return

        # Выбираем активные предприятия
        active_enterprises = [e for e in enterprises if e.get('active', False)]
        print(f"✓ Активных предприятий: {len(active_enterprises)}")

        if not active_enterprises:
            print("⚠ ВНИМАНИЕ: Нет активных предприятий. Используем все предприятия.")
            test_enterprises = enterprises[:TEST_ENTERPRISES_COUNT]
        else:
            test_enterprises = active_enterprises[:TEST_ENTERPRISES_COUNT]

        print(f"\nВыбрано для теста: {len(test_enterprises)} предприятий")

    except Exception as e:
        print(f"✗ ОШИБКА при получении списка предприятий: {e}")
        return

    # 2. Опрашиваем выбранные предприятия
    print_separator("Шаг 2: Опрос выбранных предприятий")

    # Используем последние 7 дней
    to_date = datetime.now()
    from_date = to_date - timedelta(days=7)
    from_date_str = from_date.strftime("%Y-%m-%d")
    to_date_str = to_date.strftime("%Y-%m-%d")

    print(f"Период опроса: {from_date_str} - {to_date_str}")
    print(f"Тип: daily (суточные данные)\n")

    results_summary = []

    for idx, enterprise in enumerate(test_enterprises, 1):
        print(f"\n[{idx}/{len(test_enterprises)}] Опрос: {enterprise['enterprise_name']}")
        print(f"  Line ID: {enterprise['line_id']}, SerNum: {enterprise['serNum']}, ChNum: {enterprise['chNum']}")

        try:
            data = await poll_enterprise(
                line_id=enterprise['line_id'],
                ser_num=enterprise['serNum'],
                ch_num=enterprise['chNum'],
                from_date=from_date_str,
                to_date=to_date_str,
                period_type="daily"
            )

            if not data:
                print(f"  ⚠ Нет данных за указанный период")
                results_summary.append({
                    "enterprise": enterprise['enterprise_name'],
                    "status": "no_data",
                    "records": 0
                })
                continue

            # Анализируем данные
            total_records = len(data)
            has_volume = False
            has_temperature = False
            has_pressure = False
            total_volume = 0.0

            for record in data:
                if record.get('devices'):
                    device = record['devices'][0]  # Берем первое устройство

                    if device.get('volume') and device['volume'] > 0:
                        has_volume = True
                        total_volume += device['volume']

                    if device.get('temperature') is not None:
                        has_temperature = True

                    if device.get('pressure') is not None:
                        has_pressure = True

            # Вывод результатов
            print(f"  ✓ Записей: {total_records}")
            print(f"  ✓ Суммарный объем: {total_volume:.2f} м³")
            print(f"  {'✓' if has_volume else '✗'} Данные по объему: {'Есть' if has_volume else 'Нет'}")
            print(f"  {'✓' if has_temperature else '✗'} Температура: {'Есть' if has_temperature else 'Нет'}")
            print(f"  {'✓' if has_pressure else '✗'} Давление: {'Есть' if has_pressure else 'Нет'}")

            # Показываем пример записи
            if data and data[0].get('devices'):
                sample = data[0]['devices'][0]
                print(f"  Пример данных (первая запись):")
                print(f"    - Период: {data[0]['period']}")
                print(f"    - Объем: {sample.get('volume', 'N/A')}")
                print(f"    - Температура: {sample.get('temperature', 'N/A')}")
                print(f"    - Давление: {sample.get('pressure', 'N/A')}")

            results_summary.append({
                "enterprise": enterprise['enterprise_name'],
                "status": "success",
                "records": total_records,
                "volume": total_volume,
                "has_temp": has_temperature,
                "has_press": has_pressure
            })

        except Exception as e:
            print(f"  ✗ ОШИБКА: {e}")
            results_summary.append({
                "enterprise": enterprise['enterprise_name'],
                "status": "error",
                "error": str(e)
            })

        # Небольшая задержка между запросами
        await asyncio.sleep(0.5)

    # 3. Итоговая сводка
    print_separator("Шаг 3: Итоговая сводка")

    success_count = sum(1 for r in results_summary if r['status'] == 'success')
    no_data_count = sum(1 for r in results_summary if r['status'] == 'no_data')
    error_count = sum(1 for r in results_summary if r['status'] == 'error')

    print(f"\nОбщая статистика:")
    print(f"  ✓ Успешно опрошено: {success_count}")
    print(f"  ⚠ Нет данных: {no_data_count}")
    print(f"  ✗ Ошибки: {error_count}")

    if success_count > 0:
        print(f"\nДетали успешных опросов:")
        for result in results_summary:
            if result['status'] == 'success':
                temp_icon = "✓" if result['has_temp'] else "✗"
                press_icon = "✓" if result['has_press'] else "✗"
                print(f"  • {result['enterprise'][:40]:40} | "
                      f"Записей: {result['records']:3} | "
                      f"Объем: {result['volume']:10.2f} м³ | "
                      f"T:{temp_icon} P:{press_icon}")

    if no_data_count > 0:
        print(f"\nПредприятия без данных:")
        for result in results_summary:
            if result['status'] == 'no_data':
                print(f"  • {result['enterprise']}")

    if error_count > 0:
        print(f"\nОшибки при опросе:")
        for result in results_summary:
            if result['status'] == 'error':
                print(f"  • {result['enterprise']}: {result.get('error', 'Unknown error')}")

    print_separator()

    # Проверка актуальности данных
    print("\n📊 ПРОВЕРКА АКТУАЛЬНОСТИ ДАННЫХ:")
    if success_count > 0:
        volumes = [r['volume'] for r in results_summary if r['status'] == 'success' and r['volume'] > 0]
        if len(volumes) > 1:
            unique_volumes = len(set(volumes))
            if unique_volumes == 1:
                print("  ⚠ ВНИМАНИЕ: Все предприятия имеют одинаковый объем!")
                print(f"    Возможно, бекенд не обновлен или данные из кэша.")
                print(f"    Объем: {volumes[0]:.2f} м³")
            else:
                print(f"  ✓ Данные различаются ({unique_volumes} уникальных значений)")
                print(f"    Мин: {min(volumes):.2f} м³, Макс: {max(volumes):.2f} м³")

        # Проверка температуры и давления
        has_temp_count = sum(1 for r in results_summary if r['status'] == 'success' and r['has_temp'])
        has_press_count = sum(1 for r in results_summary if r['status'] == 'success' and r['has_press'])

        print(f"\n  Температура заполнена: {has_temp_count}/{success_count} ({has_temp_count*100//success_count if success_count > 0 else 0}%)")
        print(f"  Давление заполнено: {has_press_count}/{success_count} ({has_press_count*100//success_count if success_count > 0 else 0}%)")

    print_separator()


if __name__ == "__main__":
    print("\n🧪 Запуск теста опроса предприятий...\n")
    try:
        asyncio.run(test_enterprise_polling())
        print("\n✅ Тест завершен\n")
    except KeyboardInterrupt:
        print("\n\n⚠ Тест прерван пользователем\n")
    except Exception as e:
        print(f"\n\n✗ Критическая ошибка: {e}\n")
