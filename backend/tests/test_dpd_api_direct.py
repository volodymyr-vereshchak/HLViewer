"""
Прямой тест DPD API - обращается напрямую к внешнему API

Этот тест помогает понять:
1. Какие поля реально возвращает DPD API
2. Есть ли в ответе поля 'press' и 'temper' (или 'pressure' и 'temperature')
3. Различаются ли данные между предприятиями
4. Правильно ли работает аутентификация
"""

import asyncio
import httpx
import sys
import warnings
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# Добавляем путь к бекенду
sys.path.insert(0, str(Path(__file__).parent.parent))

# Отключаем SSL warnings
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# ============================================================================
# НАСТРОЙКИ DPD API (ХАРДКОД)
# ============================================================================
DPD_API_BASE_URL = "https://rest-direct.zp.iot.grmu.com.ua/api/v1/"
DPD_AUTH_URL = "https://auth-direct.zp.iot.grmu.com.ua/auth/login"
DPD_USERNAME = "zaporizhDirect"
DPD_PASSWORD = "xTqYaRmlYQFY"
DPD_TIMEOUT = 30

# Пути к файлам маппингов
ENTERPRISE_MAPPINGS_PATH = "data/enterprise.xlsx"
LINE_ID_MAPPINGS_PATH = "data/line_id.xlsx"

# Количество предприятий для детального анализа
TEST_ENTERPRISES_COUNT = 5

# Маппинги из enterprise_mappings.py
MF_DEV_MAPPING = {
    "РадмирТех ТОВ СП, м. Харків": 1,
    "Укргазтех ДП, м.Київ": 5,
    "ГРЕМПІС ТОВ НВП, м. Вінниця": 3,
    "Тандем ПП НВФ, м.Вінниця": 4,
}

TYPE_DEV_MAPPING = {
    1: {  # РадмирТех
        "ВЕГА-1.01": 5,
        "ВЕГА-1.01Н": 15,
        "КПЛГ-2.01Р": 4,
        "КПЛГ-1.02Р": 2,
        "КПЛГ-1.01Р": 2,
        "КПЛГ-1.02РВ": 3,
        "ВЕГА-2.01": 8,
        "ВЕГА-2.01Н": 16,
        "ВЕГА-1.02": 7,
        "КВР-1.01": 11,
        "КВР-1.02": 12,
    },
    5: {  # Укргазтех
        "ФЛОУТЕК-ТМ-2-3-4-Т": 12,
        "ФЛОУТЕК-ТМ-2-3-4": 12,
        "ФЛОУТЕК-ТМ-2-3-6": 12,
        "ФЛОУТЕК-ТМ": 15,
        "ФЛОУТЕК-ТМ-2": 15,
        "ФЛОУТЕК-ТМ-1-3-1": 15,
        "ФЛОУТЕК-ТМ-1": 9,
        "ФЛОУТЕК-ТМ-3": 2
    },
    3: {  # ГРЕМПІС
        "Універсал-02": 2,
        "Універсал-01": 1,
    },
    4: {  # Тандем
        "ТАНДЕМ-ТР": 1,
    },
}


def print_separator(title: str = "", char: str = "="):
    """Печать разделителя"""
    print("\n" + char * 80)
    if title:
        print(f"  {title}")
        print(char * 80)


def load_enterprise_mappings() -> Optional[pd.DataFrame]:
    """Загрузить маппинги предприятий из Excel файлов (как в enterprise_mappings.py)"""
    try:
        # Проверка существования файлов
        if not Path(ENTERPRISE_MAPPINGS_PATH).exists():
            print(f"✗ Файл не найден: {ENTERPRISE_MAPPINGS_PATH}")
            return None

        if not Path(LINE_ID_MAPPINGS_PATH).exists():
            print(f"✗ Файл не найден: {LINE_ID_MAPPINGS_PATH}")
            return None

        # Загрузка файлов (точно как в enterprise_mappings.py)
        enterprise_df = pd.read_excel(ENTERPRISE_MAPPINGS_PATH, skiprows=3)
        line_id_df = pd.read_excel(LINE_ID_MAPPINGS_PATH, header=None)

        # Создание маппинга сектор -> line_id
        line_id_df.columns = ["sector_name", "line_id"]
        sector_to_line = line_id_df.set_index("sector_name")["line_id"].to_dict()

        # Преобразование данных (точно как в enterprise_mappings.py)
        df = enterprise_df.copy()

        # serNum: очистка от "-" и удаление ведущих нулей
        df["serNum"] = df["Прилад обліку.Номер приладу"].astype(str).str.replace("-", "", regex=False).str.lstrip("0")

        # mfDev: маппинг производителя
        df["mfDev"] = df["Прилад обліку.Тип приладу обліку.Виробник"].map(MF_DEV_MAPPING)

        # typeDev: маппинг типа устройства
        df["typeDev"] = df.apply(
            lambda row: TYPE_DEV_MAPPING.get(row["mfDev"], {}).get(row["Прилад обліку.Тип приладу обліку"])
            if pd.notna(row["mfDev"]) else None,
            axis=1
        )

        # chNum: вычитаем 1 (индексация с 0)
        df["chNum"] = df["Прилад обліку.Номер каналу (нумерація з 1)"] - 1

        # enterprise_name
        df["enterprise_name"] = df["Точка обліку"]

        # line_id: через маппинг секторов
        df["line_id"] = df["Точка обліку.Сектор"].map(sector_to_line)

        # active: 1 = активный, 0 = неактивный
        df["active"] = df["Active"] == 1

        # Очистка данных - удаляем строки без line_id
        df = df.dropna(subset=["line_id"])

        # Удаляем строки без mfDev или typeDev
        df = df.dropna(subset=["mfDev", "typeDev"])

        # Выбираем нужные колонки
        result_df = df[["line_id", "serNum", "mfDev", "typeDev", "chNum", "enterprise_name", "active"]].copy()

        # Конвертация типов
        result_df["line_id"] = result_df["line_id"].astype(int)
        result_df["serNum"] = result_df["serNum"].astype(int)
        result_df["mfDev"] = result_df["mfDev"].astype(int)
        result_df["typeDev"] = result_df["typeDev"].astype(int)
        result_df["chNum"] = result_df["chNum"].astype(int)
        result_df["active"] = result_df["active"].astype(bool)

        print(f"✓ Загружено {len(result_df)} маппингов предприятий ({len(result_df[result_df['active']])} активных)")

        return result_df

    except Exception as e:
        print(f"✗ Ошибка загрузки маппингов: {e}")
        import traceback
        traceback.print_exc()
        return None


class DPDAPITester:
    """Класс для прямого тестирования DPD API (полностью имитирует DPDClient)"""

    def __init__(self):
        self.base_url = DPD_API_BASE_URL
        self.auth_url = DPD_AUTH_URL
        self.username = DPD_USERNAME
        self.password = DPD_PASSWORD
        self.timeout = DPD_TIMEOUT

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._authenticated: bool = False

    async def authenticate(self) -> bool:
        """Аутентификация в DPD API (точно как в DPDClient)"""
        print_separator("Шаг 1: Аутентификация в DPD API")
        print(f"Auth URL: {self.auth_url}")
        print(f"Base URL: {self.base_url}")
        print(f"Username: {self.username}")

        payload = {
            "login": self.username,
            "password": self.password
        }

        try:
            async with httpx.AsyncClient(
                verify=False,
                timeout=self.timeout,
                trust_env=False  # Игнорируем HTTP_PROXY
            ) as client:
                response = await client.post(self.auth_url, json=payload)

                if response.status_code == 200:
                    data = response.json()

                    # DPD API возвращает "access" и "refresh" (не "access_token"!)
                    self.access_token = data.get("access")
                    self.refresh_token = data.get("refresh")
                    self._authenticated = True

                    print("✓ Аутентификация успешна!")
                    print(f"  Access token получен: {self.access_token[:30] if self.access_token else 'None'}...")
                    print(f"  Refresh token получен: {self.refresh_token[:30] if self.refresh_token else 'None'}...")
                    return True
                else:
                    print(f"✗ Ошибка аутентификации: {response.status_code}")
                    print(f"  Response: {response.text}")
                    return False

        except Exception as e:
            print(f"✗ Исключение при аутентификации: {e}")
            return False

    async def get_device_indications(self, device: Dict, from_date: str, to_date: str,
                                    type_request: str = "daily") -> Optional[Dict]:
        """Получить показания для устройства (точно как в DPDClient)"""

        endpoint = f"{self.base_url}indications"

        params = {
            "from": from_date,
            "to": to_date,
            "serNUM": device["serNum"],
            "mfDEV": device["mfDev"],
            "typeDEV": device["typeDev"],
            "chNUM": device["chNum"],
            "typeRequest": type_request
        }

        headers = {"Authorization": f"Bearer {self.access_token}"}

        try:
            async with httpx.AsyncClient(
                verify=False,
                timeout=60.0,
                trust_env=False
            ) as client:
                response = await client.get(endpoint, headers=headers, params=params)

                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"  ✗ Ошибка: {response.status_code} - {response.text[:100]}")
                    return None

        except Exception as e:
            print(f"  ✗ Исключение: {e}")
            return None


async def analyze_dpd_response():
    """Основная функция анализа DPD API"""

    print_separator("ПРЯМОЙ ТЕСТ DPD API", "=")
    print(f"Дата теста: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\nНастройки DPD API:")
    print(f"  DPD_API_BASE_URL: {DPD_API_BASE_URL}")
    print(f"  DPD_AUTH_URL: {DPD_AUTH_URL}")
    print(f"  DPD_USERNAME: {DPD_USERNAME}")
    print(f"  DPD_TIMEOUT: {DPD_TIMEOUT}")

    # Создаем тестер
    tester = DPDAPITester()

    # Аутентификация
    if not await tester.authenticate():
        print("\n✗ Не удалось аутентифицироваться. Проверьте учетные данные в начале скрипта")
        return

    # Загрузка маппингов предприятий
    print_separator("Шаг 2: Загрузка маппингов предприятий")

    df = load_enterprise_mappings()
    if df is None or df.empty:
        print("✗ Не удалось загрузить маппинги предприятий")
        return

    # Выбираем активные предприятия
    active_df = df[df['active'] == True]
    if len(active_df) == 0:
        print("⚠ Нет активных предприятий, используем все")
        test_df = df.head(TEST_ENTERPRISES_COUNT)
    else:
        test_df = active_df.head(TEST_ENTERPRISES_COUNT)

    print(f"Выбрано для теста: {len(test_df)} предприятий\n")

    # Период опроса
    to_date = datetime.now()
    from_date = to_date - timedelta(days=7)
    from_date_str = from_date.strftime("%Y-%m-%d")
    to_date_str = to_date.strftime("%Y-%m-%d")

    print_separator("Шаг 3: Прямой опрос DPD API")
    print(f"Период: {from_date_str} - {to_date_str}")
    print(f"Тип: daily\n")

    results = []

    for idx, row in test_df.iterrows():
        device = {
            "serNum": int(row["serNum"]),
            "mfDev": int(row["mfDev"]),
            "typeDev": int(row["typeDev"]),
            "chNum": int(row["chNum"]),
        }

        enterprise_name = row["enterprise_name"]

        print(f"[{idx + 1}/{len(test_df)}] {enterprise_name}")
        print(f"  Device: serNum={device['serNum']}, mfDev={device['mfDev']}, typeDev={device['typeDev']}, chNum={device['chNum']}")

        response = await tester.get_device_indications(
            device, from_date_str, to_date_str, "daily"
        )

        if response is None:
            print("  ⚠ Нет ответа от API\n")
            results.append({
                "enterprise": enterprise_name,
                "device": device,
                "status": "no_response"
            })
            continue

        # Анализируем структуру ответа
        print(f"  ✓ Получен ответ от DPD API")

        # Показываем структуру
        if "table" in response and "data" in response["table"]:
            data = response["table"]["data"]
            print(f"  ✓ Записей в table.data: {len(data)}")

            if len(data) > 0:
                # Берем первую запись для анализа
                first_record = data[0]

                # Показываем все поля
                print(f"\n  📋 СТРУКТУРА ПЕРВОЙ ЗАПИСИ:")
                print(f"  " + "-" * 76)
                for key, value in first_record.items():
                    value_str = str(value)[:50] if value is not None else "None"
                    print(f"    {key:20} = {value_str}")
                print(f"  " + "-" * 76)

                # Проверяем наличие ключевых полей
                has_press = "press" in first_record
                has_temper = "temper" in first_record
                has_pressure = "pressure" in first_record
                has_temperature = "temperature" in first_record

                has_dvstAlwrk = "dvstAlwrk" in first_record
                has_volume = "volume" in first_record

                print(f"\n  🔍 ПРОВЕРКА ПОЛЕЙ:")
                print(f"    {'✓' if has_press else '✗'} Поле 'press' {'найдено' if has_press else 'НЕ найдено'}")
                print(f"    {'✓' if has_temper else '✗'} Поле 'temper' {'найдено' if has_temper else 'НЕ найдено'}")
                print(f"    {'✓' if has_pressure else '✗'} Поле 'pressure' {'найдено' if has_pressure else 'НЕ найдено'}")
                print(f"    {'✓' if has_temperature else '✗'} Поле 'temperature' {'найдено' if has_temperature else 'НЕ найдено'}")
                print(f"    {'✓' if has_dvstAlwrk else '✗'} Поле 'dvstAlwrk' {'найдено' if has_dvstAlwrk else 'НЕ найдено'}")
                print(f"    {'✓' if has_volume else '✗'} Поле 'volume' {'найдено' if has_volume else 'НЕ найдено'}")

                # Вычисляем суммарный объем
                total_volume = sum(r.get("dvstAlwrk", 0) or 0 for r in data)

                print(f"\n  💾 ДАННЫЕ:")
                print(f"    Суммарный объем (dvstAlwrk): {total_volume:.2f} м³")

                # Значения температуры и давления
                press_val = first_record.get("press", first_record.get("pressure", "N/A"))
                temper_val = first_record.get("temper", first_record.get("temperature", "N/A"))

                print(f"    Давление (1-я запись): {press_val}")
                print(f"    Температура (1-я запись): {temper_val}")

                results.append({
                    "enterprise": enterprise_name,
                    "device": device,
                    "status": "success",
                    "records_count": len(data),
                    "total_volume": total_volume,
                    "has_press": has_press,
                    "has_temper": has_temper,
                    "has_pressure": has_pressure,
                    "has_temperature": has_temperature,
                    "first_record": first_record,
                    "press_value": press_val,
                    "temper_value": temper_val
                })
            else:
                print(f"  ⚠ Массив data пуст")
                results.append({
                    "enterprise": enterprise_name,
                    "device": device,
                    "status": "empty_data"
                })
        else:
            print(f"  ⚠ Неожиданная структура ответа")
            print(f"  Ключи в ответе: {list(response.keys())}")
            results.append({
                "enterprise": enterprise_name,
                "device": device,
                "status": "unexpected_structure",
                "response_keys": list(response.keys())
            })

        print()  # Пустая строка
        await asyncio.sleep(0.5)  # Небольшая задержка между запросами

    # Итоговый анализ
    print_separator("Шаг 4: Итоговый анализ")

    success_results = [r for r in results if r["status"] == "success"]

    if len(success_results) == 0:
        print("✗ Нет успешных результатов для анализа")
        return

    print(f"\nУспешно опрошено: {len(success_results)}/{len(results)}\n")

    # Проверка полей
    has_press_count = sum(1 for r in success_results if r.get("has_press", False))
    has_temper_count = sum(1 for r in success_results if r.get("has_temper", False))
    has_pressure_count = sum(1 for r in success_results if r.get("has_pressure", False))
    has_temperature_count = sum(1 for r in success_results if r.get("has_temperature", False))

    print("📊 СТАТИСТИКА ПО ПОЛЯМ:")
    print(f"  Поле 'press':       {has_press_count}/{len(success_results)} записей")
    print(f"  Поле 'temper':      {has_temper_count}/{len(success_results)} записей")
    print(f"  Поле 'pressure':    {has_pressure_count}/{len(success_results)} записей")
    print(f"  Поле 'temperature': {has_temperature_count}/{len(success_results)} записей")

    # Какие поля использовать
    print("\n💡 РЕКОМЕНДАЦИЯ ДЛЯ БЕКЕНДА:")
    if has_press_count > 0 or has_temper_count > 0:
        print("  ✓ DPD API возвращает поля 'press' и 'temper'")
        print("  ✓ В backend/api/endpoints/enterprise_ep.py должно быть:")
        print("     temperature = record.get('temper')   # НЕ 'temperature'!")
        print("     pressure = record.get('press')       # НЕ 'pressure'!")
        print("\n  ⚠ НУЖНО ОБНОВИТЬ БЕКЕНД!")
    elif has_pressure_count > 0 or has_temperature_count > 0:
        print("  ⚠ DPD API возвращает поля 'pressure' и 'temperature'")
        print("  ⚠ Бекенд уже правильный, проблема в другом")
    else:
        print("  ✗ DPD API не возвращает ни 'press/temper', ни 'pressure/temperature'")
        print("  ✗ Возможно, эти данные недоступны в API")

    # Проверка объемов
    volumes = [r["total_volume"] for r in success_results]
    unique_volumes = set(volumes)

    print("\n📈 ПРОВЕРКА РАЗЛИЧИЯ ДАННЫХ:")
    if len(unique_volumes) == 1:
        print(f"  ⚠ Все предприятия имеют ОДИНАКОВЫЙ объем: {volumes[0]:.2f} м³")
        print(f"  ⚠ Это проблема DPD API или настроек устройств, НЕ бекенда!")
    else:
        print(f"  ✓ Данные различаются: {len(unique_volumes)} уникальных значений")
        print(f"    Мин: {min(volumes):.2f} м³")
        print(f"    Макс: {max(volumes):.2f} м³")

    # Детали по каждому предприятию
    print("\n📋 ДЕТАЛИ ПО ПРЕДПРИЯТИЯМ:")
    for r in success_results:
        print(f"  • {r['enterprise'][:45]:45}")
        print(f"    Объем: {r['total_volume']:10.2f} м³  |  " +
              f"Давление: {r.get('press_value', 'N/A'):>8}  |  " +
              f"Температура: {r.get('temper_value', 'N/A'):>8}")

    print_separator()


if __name__ == "__main__":
    print("\n🔬 Запуск прямого теста DPD API...\n")

    try:
        asyncio.run(analyze_dpd_response())
        print("\n✅ Тест завершен\n")
    except KeyboardInterrupt:
        print("\n\n⚠ Тест прерван пользователем\n")
    except Exception as e:
        print(f"\n\n✗ Критическая ошибка: {e}\n")
        import traceback
        traceback.print_exc()
