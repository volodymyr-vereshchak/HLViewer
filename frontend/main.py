import flet as ft

from controls.daily_controls import DayArchiveTab


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

    daily_archive = DayArchiveTab(icon_size=icon_size)

    def main_tab_click(e):
        if int(e.data) == 1:
            daily_archive.gas_vol_container.update_list_of_gas_calc()

    main_tabs = ft.Tabs(
        [settings, daily_archive, hourly_archive, edit_archive],
        selected_index=0,
        expand=True,
        label_padding=10,
        on_change=main_tab_click,
    )

    page.add(main_tabs)


ft.app(target=main)
