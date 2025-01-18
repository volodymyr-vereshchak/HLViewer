import flet as ft

from controls.daily_controls import get_daily_archive_tab


def main(page: ft.Page):
    page.title = "HostlibViewer"
    icon_size = {"width": 56, "height": 56}
    settings = ft.Tab(
        text="",
        content=ft.Text("Settings"),
        icon=ft.Image(
            src="D:/Projects/HLViewer/HLViewer/frontend/src/assets/settings-svgrepo-com.svg",
            width=icon_size["width"],
            height=icon_size["height"],
            tooltip="Settings",
        ),
    )

    hourly_archive = ft.Tab(
        text="",
        content=ft.Text("Hourly archive"),
        icon=ft.Image(
            src="D:/Projects/HLViewer/HLViewer/frontend/src/assets/clocks-svgrepo-com.svg",
            width=icon_size["width"],
            height=icon_size["height"],
            tooltip="Hourly archive",
        ),
    )

    edit_archive = ft.Tab(
        text="",
        content=ft.Text("Edit archive"),
        icon=ft.Image(
            src="D:/Projects/HLViewer/HLViewer/frontend/src/assets/clipboard-svgrepo-com.svg",
            width=icon_size["width"],
            height=icon_size["height"],
            tooltip="Edit archive",
        ),
    )

    daily_archive = get_daily_archive_tab(icon_size)

    main_tabs = ft.Tabs(
        [settings, daily_archive, hourly_archive, edit_archive],
        selected_index=0,
        expand=True,
        label_padding=10,
    )

    page.add(main_tabs)


ft.app(target=main)
