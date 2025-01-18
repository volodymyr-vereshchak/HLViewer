from datetime import datetime

import flet as ft


def get_daily_archive_tab(page, icon_size: int) -> ft.Tab:
    daily_table = ft.DataTable(
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
    # Пример данных для списка
    gas_volumes = ["Volume 1", "Volume 2", "Volume 3"]

    # Функция обработки клика
    def on_item_click(e):
        print(f"Clicked on {e.control.content.value}")

    # Список кликабельных элементов
    list_items = [
        ft.Container(
            content=ft.Text(name),
            padding=5,
            bgcolor=ft.Colors.BLACK,
            ink=True,  # Для визуального эффекта при клике
            on_click=on_item_click,  # Обработчик клика
        )
        for name in gas_volumes
    ]

    # Интерфейс
    daily_content = ft.Row(
        controls=[
            ft.Container(
                content=ft.Column(list_items),  # Используем Column для списка элементов
                padding=10,
                bgcolor=ft.Colors.BLACK,
            ),
            ft.Container(
                content=daily_table,
                padding=10,
                bgcolor=ft.Colors.BLACK,
                expand=1,  # Занимает оставшееся место
            ),
        ],
        expand=True,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    def change_start_date(e, button):
        button.text = f"{e.control.value.strftime('%d-%m-%Y')}"
        page.update()

    start_date_picker = ft.ElevatedButton(
        datetime.today().strftime("%d-%m-%Y"),
        icon=ft.Icons.CALENDAR_MONTH,
        on_click=lambda e: page.open(
            ft.DatePicker(
                current_date=datetime.today(),
                first_date=datetime(
                    2020,
                    1,
                    1,
                ),
                last_date=datetime(2050, 1, 1),
                on_change=lambda e: change_start_date(e, start_date_picker),
            )
        ),
    )

    end_date_picker = ft.ElevatedButton(
        datetime.today().strftime("%d-%m-%Y"),
        icon=ft.Icons.CALENDAR_MONTH,
        on_click=lambda e: page.open(
            ft.DatePicker(
                current_date=datetime.today(),
                first_date=datetime(
                    2020,
                    1,
                    1,
                ),
                last_date=datetime(2050, 1, 1),
                on_change=lambda e: change_start_date(e, end_date_picker),
            )
        ),
    )

    menu_day_row = ft.Row(
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
            start_date_picker,
            end_date_picker,
            ft.Checkbox(value=False),
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    daily_column = ft.Column(
        controls=[menu_day_row, daily_content],
        expand=True,
    )

    daily_archive = ft.Tab(
        text="",
        content=daily_column,
        icon=ft.Icon(
            name=ft.Icons.TODAY,
            color=ft.Colors.WHITE,
            size=icon_size,
            tooltip="Daily archive",
        ),
    )
    return daily_archive
