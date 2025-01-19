from datetime import datetime

import flet as ft
from sqlalchemy.orm.sync import update

from api_client.daily_archive_client import DailyArchiveClient
from api_client.gas_volume_calc_client import GasVolumeCalcClient


class Calendar(ft.ElevatedButton):
    def __init__(self):
        super().__init__()
        self.text = datetime.today().strftime("%d-%m-%Y")
        self.icon = ft.Icons.CALENDAR_MONTH
        self.on_click = self.on_click_cal

    def change_date(self, e):
        self.text = f"{e.control.value.strftime('%d-%m-%Y')}"
        self.update()

    def on_click_cal(self, e):
        self.page.open(
            ft.DatePicker(
                current_date=datetime.today(),
                first_date=datetime(
                    2020,
                    1,
                    1,
                ),
                last_date=datetime(2050, 1, 1),
                on_change=self.change_date,
            )
        )


class GasVolumeNameContainer(ft.Container):
    def __init__(self, gas_volume_data: dict, content, on_click):
        super().__init__(content=content, on_click=on_click)
        self.gas_volume_data = gas_volume_data
        self.padding = 5
        self.ink = True
        self.bgcolor = ft.Colors.BLACK


class GasVolumesContainer(ft.Container):
    def __init__(self, data_table_instance: "DayArchiveTable"):
        super().__init__()
        self.table_instance = data_table_instance
        self.gas_volumes = self.get_gas_volumes()
        self.content = ft.Column(
            [
                GasVolumeNameContainer(
                    gas_volume_data=gas_volume,
                    content=ft.Text(gas_volume["name"]),
                    on_click=self.con_click,
                )
                for gas_volume in self.gas_volumes
            ]
        )
        self.padding = 10
        self.bgcolor = ft.Colors.BLACK

    @staticmethod
    def get_gas_volumes():
        status, gas_volumes = GasVolumeCalcClient().api_request()
        if not status:
            pass
        return gas_volumes

    def update_list_of_gas_calc(self):
        self.content = ft.Column(
            [
                GasVolumeNameContainer(
                    gas_volume_data=gas_volume,
                    content=ft.Text(gas_volume["name"]),
                    on_click=self.con_click,
                )
                for gas_volume in self.gas_volumes
            ],
            scroll=ft.ScrollMode.AUTO,
        )
        self.update()

    def con_click(self, e):
        gas_volume_calc_id = e.control.gas_volume_data["id"]
        self.table_instance.update_table_by_gas_flow_calc_id(gas_volume_calc_id)


class DayArchiveTable(ft.DataTable):
    def __init__(
        self,
        columns: list[ft.DataColumn],
        rows: list[ft.DataRow] = None,
        table_data: list[dict] = None,
        gas_flow_data: dict = None,
    ):
        super().__init__(rows=rows, columns=columns)
        self.table_data = table_data
        self.gas_flow_data = gas_flow_data
        self.horizontal_lines = ft.BorderSide(color=ft.Colors.WHITE, width=2)
        self.vertical_lines = ft.BorderSide(color=ft.Colors.WHITE, width=2)

    @staticmethod
    def get_daily_archive_data(gas_volume_calc_id: int):
        status, daily_archive_data = DailyArchiveClient(
            gas_volume_calc_id=gas_volume_calc_id
        ).api_request()
        return daily_archive_data

    @staticmethod
    def get_rows_data(data: list[dict]):
        rows = [
            ft.DataRow(
                cells=[ft.DataCell(ft.Text(str(value))) for value in row.values()]
            )
            for row in data
        ]
        return rows

    def update_table_by_gas_flow_calc_id(self, gas_volume_calc_id: int):
        day_archive_data_list = self.get_daily_archive_data(
            gas_volume_calc_id=gas_volume_calc_id
        )
        day_archive_data = [
            {
                key: volume
                for key, volume in day_archive_dict.items()
                if key not in ["id", "gas_vol_calc_id"]
            }
            for day_archive_dict in day_archive_data_list
        ]

        self.rows = self.get_rows_data(day_archive_data)
        self.update()


class DayArchiveTab(ft.Tab):
    def __init__(self, icon_size: int):
        super().__init__()
        self.icon = ft.Icon(
            name=ft.Icons.TODAY,
            color=ft.Colors.WHITE,
            size=icon_size,
            tooltip="Daily archive",
        )
        self.daily_table = DayArchiveTable(columns=self.get_columns_data())

        self.gas_vol_container = GasVolumesContainer(self.daily_table)

        self.daily_content = ft.Row(
            controls=[
                self.gas_vol_container,
                ft.Column(
                    controls=[self.daily_table], expand=True, scroll=ft.ScrollMode.AUTO
                ),
            ],
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        self.start_date_picker = Calendar()
        self.end_date_picker = Calendar()
        self.date_checkbox = ft.Checkbox(value=False)
        self.menu_day_row = ft.Row(
            controls=[
                ft.IconButton(
                    icon=ft.Icons.LOCAL_PRINT_SHOP_OUTLINED,
                    icon_color=ft.Colors.WHITE,
                    icon_size=icon_size,
                ),
                ft.IconButton(
                    icon=ft.Icons.TABLE_CHART,
                    icon_color=ft.Colors.WHITE,
                    icon_size=icon_size,
                ),
                self.start_date_picker,
                self.end_date_picker,
                self.date_checkbox,
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.daily_column = ft.Column(
            controls=[self.menu_day_row, self.daily_content],
            expand=True,
        )
        self.content = self.daily_column

    @staticmethod
    def get_columns_data():
        return [
            ft.DataColumn(ft.Text("Period")),
            ft.DataColumn(ft.Text("Volume"), numeric=True),
            ft.DataColumn(ft.Text("Dp"), numeric=True),
            ft.DataColumn(ft.Text("Pressure"), numeric=True),
            ft.DataColumn(ft.Text("Temperature"), numeric=True),
            ft.DataColumn(ft.Text("Density"), numeric=True),
        ]
