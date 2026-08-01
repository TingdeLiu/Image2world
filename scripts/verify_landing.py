from pathlib import Path

from playwright.sync_api import sync_playwright


root = Path(__file__).resolve().parents[1]
artifacts = root / ".artifacts"
artifacts.mkdir(exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    errors: list[str] = []

    desktop = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    desktop.set_default_timeout(120_000)
    desktop.on("console", lambda message: errors.append(f"console:{message.type}:{message.text}") if message.type == "error" else None)
    desktop.on("pageerror", lambda error: errors.append(f"page:{error}"))
    desktop.goto("http://127.0.0.1:3000", wait_until="domcontentloaded", timeout=120_000)
    desktop.get_by_role("heading", name="Step inside your image.").wait_for()
    desktop.screenshot(path=artifacts / "landing-desktop.png", full_page=True)
    desktop.get_by_role("button", name="Build from an image").click()
    desktop.get_by_role("dialog").wait_for()
    modal_title = desktop.get_by_role("heading", name="Create New World").is_visible()
    desktop.keyboard.press("Escape")

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.set_default_timeout(120_000)
    mobile.on("console", lambda message: errors.append(f"mobile-console:{message.type}:{message.text}") if message.type == "error" else None)
    mobile.on("pageerror", lambda error: errors.append(f"mobile-page:{error}"))
    mobile.goto("http://127.0.0.1:3000", wait_until="domcontentloaded", timeout=120_000)
    mobile.get_by_role("heading", name="Step inside your image.").wait_for()
    mobile.screenshot(path=artifacts / "landing-mobile.png", full_page=True)

    print({
        "title": desktop.title(),
        "desktop_heading": desktop.get_by_role("heading", name="Step inside your image.").is_visible(),
        "mobile_heading": mobile.get_by_role("heading", name="Step inside your image.").is_visible(),
        "create_modal": modal_title,
        "errors": errors,
    })
    browser.close()
