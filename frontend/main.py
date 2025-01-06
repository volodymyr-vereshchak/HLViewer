import flet as ft


def main(page: ft.Page):
    page.title = "Split screen with independent tabs"
    page.window.width = 800
    page.window.height = 600
    page.horizontal_alignment = "center"
    page.vertical_alignment = "center"

    # Tabs for the first section
    tabs1 = ft.Tabs(
        tabs=[
            ft.Tab(text="Tab 1-1", content=ft.Text("Content of Tab 1-1")),
            ft.Tab(text="Tab 1-2", content=ft.Text("Content of Tab 1-2")),
        ],
        selected_index=0,
        expand=True,
    )

    # Tabs for the second section
    tabs2 = ft.Tabs(
        tabs=[
            ft.Tab(text="Tab 2-1", content=ft.Text("Content of Tab 2-1")),
            ft.Tab(text="Tab 2-2", content=ft.Text("Content of Tab 2-2")),
        ],
        selected_index=0,
        expand=True,
    )

    # Two vertical sections in a Row
    tab_content = ft.Row(
        controls=[
            ft.Container(
                content=tabs1,
                padding=10,
                bgcolor=ft.Colors.LIGHT_BLUE_50,
                expand=1,
            ),
            ft.Container(
                content=tabs2,
                padding=10,
                bgcolor=ft.Colors.LIGHT_GREEN_50,
                expand=2,
            ),
        ],
        expand=True,
    )

    main_tab = ft.Tabs(
        tabs=[
            ft.Tab(
                text="Settings",
                content=tab_content,
                icon=ft.Image(
                    src="D:/Projects/HLViewer/HLViewer/frontend/src/assets/settings-svgrepo-com.svg",
                    width=32,
                    height=32,
                ),
            ),
            ft.Tab(text="Tab 0-2", content=ft.Text("Content of Tab 0-2")),
        ],
        selected_index=0,
        expand=True,
    )

    page.add(main_tab)


ft.app(target=main)
