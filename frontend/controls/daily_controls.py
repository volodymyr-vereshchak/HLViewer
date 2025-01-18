import flet as ft


def get_daily_archive_tab(icon_size: int) -> ft.Tab:
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

    daily_archive = ft.Tab(
        text="",
        content=daily_content,
        icon=ft.Icon(
            name=ft.Icons.TODAY,
            color=ft.Colors.WHITE,
            size=icon_size,
            tooltip="Daily archive",
        ),
    )
    return daily_archive
