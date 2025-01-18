import flet as ft
from blib2to3.pgen2.tokenize import colon

from controls.daily_controls import get_daily_archive_tab


def main(page: ft.Page):
    page.title = "HostlibViewer"
    icon_size = 56
    settings = ft.Tab(
        text="",
        content=ft.Text("Settings"),
        icon=ft.Icon(
            name=ft.Icons.SETTINGS_OUTLINED, color=ft.Colors.WHITE, size=icon_size
        ),
    )

    hourly_archive = ft.Tab(
        text="",
        content=ft.Text("Hourly archive"),
        icon=ft.Icon(name=ft.Icons.ACCESS_TIME, color=ft.Colors.WHITE, size=icon_size),
    )

    edit_archive = ft.Tab(
        text="",
        content=ft.Text("Edit archive"),
        icon=ft.Icon(name=ft.Icons.EDIT_NOTE, color=ft.Colors.WHITE, size=icon_size),
    )

    daily_archive = get_daily_archive_tab(page, icon_size)

    main_tabs = ft.Tabs(
        [settings, daily_archive, hourly_archive, edit_archive],
        selected_index=0,
        expand=True,
        label_padding=10,
    )

    page.add(main_tabs)


ft.app(target=main, assets_dir="src/assets")
