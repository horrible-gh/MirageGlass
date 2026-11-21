"""Prevent sideways scrolling in the project sidebar when a group name is long."""

from pathlib import Path

from playwright.sync_api import sync_playwright

CSS_PATH = Path(__file__).resolve().parents[1] / "web" / "app.css"


def test_project_sidebar_scans_vertically_without_horizontal_overflow():
    css = CSS_PATH.read_text(encoding="utf-8")
    labels = [
        "TimeWeaver",
        "FlowGate",
        "MirageGlass",
        "ToolHub",
        "FlowGate v0.2",
        "An extremely long project name with multiple screen versions " * 5,
    ]
    rows = "".join(
        '<div class="project-item">'
        f'<button class="side-link" aria-label="{labels[i % len(labels)]}">'
        '<span class="project-dot"></span>'
        f'<span class="project-name">{labels[i % len(labels)]}</span>'
        f'<span class="count">{i + 100}</span>'
        '</button>'
        '<button class="lib-group-kebab" aria-label="Group actions">⋮</button>'
        '</div>'
        for i in range(30)
    )
    html = (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        + css
        + '</style></head><body><div class="shell"><aside class="sidebar">'
        + '<div class="side-heading"><span class="side-label">Projects</span></div>'
        + '<div id="project-nav">' + rows + '</div>'
        + '<div class="sidebar-subsection"><button class="side-link">Ungrouped</button></div>'
        + '<div class="sidebar-spacer"></div></aside><main class="main"></main></div>'
        + '</body></html>'
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 600})
        page.set_content(html)
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 600})
            measurements = page.evaluate("""() => {
                const nav = document.querySelector('#project-nav');
                const rows = Array.from(nav.querySelectorAll('.project-item'));
                const buttons = rows.map(row => row.querySelector('.lib-group-kebab'));
                return {
                    scrollWidth: nav.scrollWidth,
                    clientWidth: nav.clientWidth,
                    scrollHeight: nav.scrollHeight,
                    clientHeight: nav.clientHeight,
                    overflowX: getComputedStyle(nav).overflowX,
                    misplacedButtons: rows.filter((row, i) =>
                        buttons[i].getBoundingClientRect().right >
                        row.getBoundingClientRect().right + 1).length,
                    clippedMenus: buttons.filter(btn =>
                        btn.getBoundingClientRect().right >
                        nav.getBoundingClientRect().right + 1).length,
                    truncatedLongName: [...nav.querySelectorAll('.project-name')].some(
                        name => name.scrollWidth > name.clientWidth &&
                                getComputedStyle(name).textOverflow === 'ellipsis'
                    ),
                };
            }""")
            assert measurements["overflowX"] == "hidden"
            assert measurements["scrollWidth"] <= measurements["clientWidth"] + 1
            assert measurements["scrollHeight"] > measurements["clientHeight"]
            assert measurements["misplacedButtons"] == 0
            assert measurements["clippedMenus"] == 0
            assert measurements["truncatedLongName"]
        browser.close()
