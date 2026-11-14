"""GRS consumption model: reading the 1С exports, training, the detector."""
import io
from datetime import date, timedelta
from types import SimpleNamespace

import openpyxl
import pytest

from backend.services.grs_model import parse, service
from tests import grs_model_data as data

PERIOD = data.days(date(2026, 1, 1), date(2026, 1, 5))


class TestParse:
    def test_inflow_is_a_row_per_grs(self):
        parsed = parse.parse(data.inflow_xlsx(
            {"ГРС Альфа": {PERIOD[0]: 10.5, PERIOD[2]: 12}, "ГРС Бета": {PERIOD[1]: 7}}, PERIOD))
        assert (parsed.kind, parsed.layout) == (parse.INFLOW, "grs")
        assert parsed.days == PERIOD
        assert sorted(parsed.rows) == [
            (PERIOD[0], "ГРС Альфа", parse.INFLOW_CATEGORY, 10.5),
            (PERIOD[1], "ГРС Бета", parse.INFLOW_CATEGORY, 7.0),
            (PERIOD[2], "ГРС Альфа", parse.INFLOW_CATEGORY, 12.0),
        ]
        assert "баланс" in parsed.title

    def test_consumption_keeps_categories_and_drops_the_grs_sum(self):
        parsed = parse.parse(data.consumption_xlsx({
            "ГРС Альфа": {"Промисловість": {PERIOD[0]: 3}, "Тепломережі": {PERIOD[0]: 4, PERIOD[1]: 5}},
            "ГРС Бета": {"Бюджет": {PERIOD[4]: 1}},
        }, PERIOD))
        assert (parsed.kind, parsed.layout) == (parse.CONSUMPTION, "category")
        assert sorted(parsed.rows) == [
            (PERIOD[0], "ГРС Альфа", "Промисловість", 3.0),
            (PERIOD[0], "ГРС Альфа", "Тепломережі", 4.0),
            (PERIOD[1], "ГРС Альфа", "Тепломережі", 5.0),
            (PERIOD[4], "ГРС Бета", "Бюджет", 1.0),
        ]

    def test_report_by_object_is_folded_to_grs_and_category(self):
        parsed = parse.parse(data.object_xlsx([
            ("Промисловість", "Завод 1", "ГРС Альфа", {PERIOD[0]: 3}),
            ("Промисловість", "Завод 2", "ГРС Альфа", {PERIOD[0]: 4}),
            ("Бюджет", "Школа", "ГРС Бета", {PERIOD[1]: 1}),
        ], PERIOD))
        assert (parsed.kind, parsed.layout) == (parse.CONSUMPTION, "object")
        assert sorted(parsed.rows) == [
            (PERIOD[0], "ГРС Альфа", "Промисловість", 7.0),
            (PERIOD[1], "ГРС Бета", "Бюджет", 1.0),
        ]

    def test_file_as_1c_writes_it_is_repaired(self):
        content = data.as_1c_writes_it(data.inflow_xlsx({"ГРС Альфа": {PERIOD[0]: 1}}, PERIOD))
        assert parse.parse(content).rows == [(PERIOD[0], "ГРС Альфа", parse.INFLOW_CATEGORY, 1.0)]

    def test_not_a_workbook(self):
        with pytest.raises(parse.ParseError, match="Excel"):
            parse.parse(b"not a zip")

    def test_workbook_without_days(self):
        book = openpyxl.Workbook()
        book.active.cell(1, 1, "Щось інше")
        out = io.BytesIO()
        book.save(out)
        with pytest.raises(parse.ParseError, match="дат"):
            parse.parse(out.getvalue())

    @pytest.mark.parametrize("raw, name", [
        ("ГРС-1 Запоріжжя", parse.RING_NAME),
        ("ГРС 3 Запоріжжя (Запорізька обл.)", parse.RING_NAME),
        ("Кільцева система м.Запоріжжя", parse.RING_NAME),
        ("ГРС Вільнянськ (Запорізька обл.)", "ГРС Вільнянськ"),
        ("ГРС  Вільнянськ", "ГРС Вільнянськ"),
        ("Приймання (ГРС Х) - Постачальник", "Приймання (ГРС Х)"),
    ])
    def test_station_names_agree_between_reports(self, raw, name):
        assert parse.grs_name(raw) == name


def _inputs(last: date = data.LAST, weather_last: date = data.LAST) -> service.Inputs:
    weather = data.weather(data.FIRST, weather_last)
    households = data.remainder(data.weather())
    ids = {name: i for i, name in enumerate(data.STATIONS, start=1)}
    period = data.days(data.FIRST, last)
    volumes = []
    for name, sid in ids.items():
        for day in period:
            volumes.append((sid, day, parse.INFLOW_CATEGORY, households[name][day] + data.INDUSTRY_VOLUME))
            volumes.append((sid, day, data.INDUSTRY, data.INDUSTRY_VOLUME))
    rows = [SimpleNamespace(source="forecast" if r["day"] > last else "archive", **r) for r in weather]
    return service.Inputs(
        stations=[service.Station(id=sid, name=name) for name, sid in ids.items()],
        volumes=volumes,
        covered={parse.INFLOW: period, parse.CONSUMPTION: period},
        weather={1: service.weather_frame(rows)},
    )


class TestModel:
    @pytest.fixture(scope="class")
    def trained(self):
        inputs = _inputs()
        return inputs, *service.train(inputs)

    def test_every_station_gets_a_model_that_follows_the_weather(self, trained):
        _, summary, payload = trained
        assert [s["status"] for s in summary["stations"]] == ["ok"] * 4
        for station in summary["stations"]:
            # The synthetic households answer the day's own temperature, the
            # model smooths it over days: that mismatch is all stations' alike.
            assert station["month_error"] < 8, station
            assert station["sigma_week_heat"] < 3, station
            assert station["train_days"] >= 400
        assert set(payload["stations"]) == set(data.STATIONS)

    def test_fault_does_not_go_into_the_model_and_is_found(self, trained):
        inputs, summary, payload = trained
        faulty = next(s for s in summary["stations"] if s["name"] == data.FAULTY)
        assert sum(faulty["excluded"].values()) >= 10

        result = service.analyse(inputs, payload)
        episodes = [e for e in result["episodes"] if e["grs"] == data.FAULTY]
        assert episodes, result["episodes"]
        hit = max(episodes, key=lambda e: e["days"])
        assert hit["start"] <= data.FAULT[1].isoformat() and hit["end"] >= data.FAULT[0].isoformat()
        assert hit["deviation"] > 30
        assert not [e for e in result["episodes"] if e["grs"] != data.FAULTY and e["days"] > 14]

    def test_period_narrows_what_is_returned(self, trained):
        inputs, _, payload = trained
        result = service.analyse(inputs, payload, date(2026, 2, 1), date(2026, 2, 28))
        assert result["days"][0] == "2026-02-01" and len(result["days"]) == 28
        assert all(len(s["y"]) == 28 for s in result["stations"])

    def test_forecast_days_are_predicted_without_a_fact(self):
        last = data.LAST - timedelta(days=10)
        inputs = _inputs(last=last)
        _, payload = service.train(inputs)
        result = service.analyse(inputs, payload)
        assert result["last_fact_day"] == last.isoformat()
        assert result["days"][-1] == data.LAST.isoformat()
        assert result["forecast"][-1] is True
        station = result["stations"][0]
        assert station["y"][-1] is None and station["model"][-1] is not None
        assert result["total"]["ahead"][-1] is not None

    def test_nothing_to_train_on(self):
        with pytest.raises(service.ModelError):
            service.train(service.Inputs(stations=[], volumes=[], covered={}))

    def test_too_few_days(self):
        inputs = _inputs(last=data.FIRST + timedelta(days=60))
        with pytest.raises(service.ModelError, match="чистих днів"):
            service.train(inputs)
