from datetime import datetime

import flet as ft

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
    def __init__(self):
        super().__init__()
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
        pass


class DayArchiveTab(ft.Tab):
    def __init__(self, icon_size: int):
        super().__init__()
        self.icon = ft.Icon(
            name=ft.Icons.TODAY,
            color=ft.Colors.WHITE,
            size=icon_size,
            tooltip="Daily archive",
        )
        self.daily_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("First name")),
                ft.DataColumn(ft.Text("Last name")),
                ft.DataColumn(ft.Text("Age"), numeric=True),
            ],
            rows=[
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text("John")),
                        ft.DataCell(ft.Text("Smith")),
                        ft.DataCell(ft.Text("43")),
                    ],
                ),
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text("Jack")),
                        ft.DataCell(ft.Text("Brown")),
                        ft.DataCell(ft.Text("19")),
                    ],
                ),
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text("Alice")),
                        ft.DataCell(ft.Text("Wong")),
                        ft.DataCell(ft.Text("25")),
                    ],
                ),
            ],
        )

        self.gas_vol_container = GasVolumesContainer()

        self.daily_content = ft.Row(
            controls=[
                self.gas_vol_container,
                ft.Container(
                    content=self.daily_table,
                    padding=10,
                    bgcolor=ft.Colors.BLACK,
                    expand=1,
                ),
            ],
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        self.start_date_picker = Calendar()
        self.end_date_picker = Calendar()
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
                ft.Checkbox(value=False),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.daily_column = ft.Column(
            controls=[self.menu_day_row, self.daily_content],
            expand=True,
        )
        self.content = self.daily_column
