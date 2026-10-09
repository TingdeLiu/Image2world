from pathlib import Path
import argparse

from playwright.sync_api import expect, sync_playwright


root = Path(__file__).resolve().parents[1]
artifacts = root / ".artifacts"
artifacts.mkdir(exist_ok=True)
parser = argparse.ArgumentParser(description="Verify desktop/mobile landing pages and modal keyboard navigation.")
parser.add_argument("--url", default="http://127.0.0.1:3000")
base_url = parser.parse_args().url.rstrip("/")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    errors: list[str] = []

    def isolate_generation(page):
        # UI checks must not attach to an operator's running job or need a GPU.
        page.route("**/api/generate", lambda route: route.fulfill(json={"ready": True}))
        page.route("**/api/generate/jobs", lambda route: route.fulfill(json={"jobs": [], "active": None}))

    desktop = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    isolate_generation(desktop)
    desktop.set_default_timeout(120_000)
    desktop.on("console", lambda message: errors.append(f"console:{message.type}:{message.text}") if message.type == "error" else None)
    desktop.on("pageerror", lambda error: errors.append(f"page:{error}"))
    desktop.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    desktop.wait_for_load_state("networkidle")
    desktop.get_by_role("heading", name="Step inside your image.").wait_for()
    desktop.screenshot(path=artifacts / "landing-desktop.png", full_page=True)
    desktop.get_by_role("button", name="Build from an image").click()
    desktop.get_by_role("dialog").wait_for()
    modal_title = desktop.get_by_role("heading", name="Create New World").is_visible()
    assert modal_title, "The create-world modal did not open"
    desktop.get_by_role("button", name="Close modal").focus()
    for _ in range(12):
        desktop.keyboard.press("Tab")
        assert desktop.evaluate('!!document.activeElement.closest("[role=dialog]")'), "Tab escaped the modal"
    desktop.get_by_role("button", name="Close modal").focus()
    desktop.keyboard.press("Shift+Tab")
    assert desktop.evaluate('!!document.activeElement.closest("[role=dialog]")'), "Shift+Tab escaped the modal"
    desktop.keyboard.press("Escape")
    expect(desktop.get_by_role("dialog")).to_have_count(0)
    expect(desktop.get_by_role("button", name="Build from an image")).to_be_focused()
    assert desktop.evaluate('document.body.style.overflow') != "hidden", "Closing the modal did not restore scrolling"

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    isolate_generation(mobile)
    mobile.set_default_timeout(120_000)
    mobile.on("console", lambda message: errors.append(f"mobile-console:{message.type}:{message.text}") if message.type == "error" else None)
    mobile.on("pageerror", lambda error: errors.append(f"mobile-page:{error}"))
    mobile.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    mobile.wait_for_load_state("networkidle")
    mobile.get_by_role("heading", name="Step inside your image.").wait_for()
    mobile.screenshot(path=artifacts / "landing-mobile.png", full_page=True)
    assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth'), "Mobile landing page overflows horizontally"
    mobile.get_by_role("button", name="Build from an image").click()
    expect(mobile.get_by_role("dialog")).to_be_visible()
    assert mobile.evaluate('document.body.style.overflow') == "hidden", "Background scrolling is not locked"
    mobile.locator("#file-upload").set_input_files({"name": "bad.txt", "mimeType": "text/plain", "buffer": b"invalid"})
    expect(mobile.get_by_role("dialog").get_by_role("alert")).to_contain_text("Choose a PNG")
    expect(mobile.get_by_role("button", name="Generate World")).to_be_disabled()
    mobile.keyboard.press("Escape")
    expect(mobile.get_by_role("dialog")).to_have_count(0)
    assert not errors, f"Browser errors: {errors}"

    print({
        "title": desktop.title(),
        "desktop_heading": desktop.get_by_role("heading", name="Step inside your image.").is_visible(),
        "mobile_heading": mobile.get_by_role("heading", name="Step inside your image.").is_visible(),
        "create_modal": modal_title,
        "errors": errors,
    })
    browser.close()
