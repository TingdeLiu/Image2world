"""Verify staged-video UI without running models, uploading videos or publishing assets."""
import argparse
import os
import re
import struct
import zlib
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:3000")
BASE = parser.parse_args().url.rstrip("/")
OUT = ROOT / ".artifacts/preview-ui-verification"
OUT.mkdir(parents=True, exist_ok=True)
validation = {"protocol": "held-out-rgb-v1", "status": "measured", "train_views": 8, "evaluation_views": 2,
              "evaluation_split": "val", "metrics": {"psnr": 20.0, "ssim": 0.7, "lpips": 0.3}}
testing = {**validation, "evaluation_split": "test"}


def png_chunk(name, value):
    return struct.pack("!I", len(value)) + name + value + struct.pack("!I", zlib.crc32(name + value))


pixel = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack("!2I5B", 4, 1, 8, 2, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(b"\x00" + b"\x80\x80\x80" * 4)) + png_chunk(b"IEND", b"")
jobs, actions, published = {}, [], []
publish_attempts = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 950})
    page.set_default_timeout(30000)
    page.route("**/api/generate", lambda r: r.fulfill(json={"ready": True}))
    page.route("**/api/generate/jobs", lambda r: r.fulfill(json={"active": None, "jobs": []}))
    # Keep background 3D rendering out of this form/workflow test.
    page.route("**/*.spz", lambda r: r.abort())

    def backend(route):
        request = route.request
        path = urlparse(request.url).path
        headers = {"access-control-allow-origin": BASE, "access-control-allow-methods": "GET, POST, DELETE, OPTIONS", "access-control-allow-headers": "*"}
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=headers)
            return
        parts = path.split("/")[3:]
        if not parts or not parts[0]:
            if request.method == "GET":
                response = {"jobs": list(jobs.values())}
            else:
                body = request.post_data_buffer.decode("utf-8", errors="replace")
                staged = bool(re.search(r'name="preview_first"\s*\r?\n\r?\ntrue', body))
                job_id = f"{len(jobs) + 1:012x}"
                response = {"id": job_id, "name": "Browser staged room", "status": "running", "stage": "training", "progress": 40,
                            "preview_first": staged, "phase": "preview" if staged else "final", "quality_report": validation}
                jobs[job_id] = response
        else:
            job = jobs[parts[0]]
            if len(parts) > 1 and parts[1] == "preview":
                route.fulfill(body=pixel, content_type="image/png", headers=headers)
                return
            if request.method == "DELETE":
                job["status"] = "canceled"
            elif request.method == "POST":
                action = parts[1]
                assert job["status"] == "awaiting_review", "Cannot skip the durable preview decision"
                actions.append(action)
                job.update(status="running", phase="final", decision=action, stage="training", progress=30)
            elif job["status"] == "running":
                if job["phase"] == "preview":
                    job.update(status="awaiting_review", stage="awaiting_review", progress=100)
                else:
                    job.update(status="complete", stage="complete", progress=100, quality_report=testing)
            response = job
        route.fulfill(json=response, headers=headers)

    page.route("**/api/reconstruction-jobs**", backend)

    def finalize(route):
        job = jobs[route.request.post_data_json["jobId"]]
        assert job["status"] == "complete"
        assert not job["preview_first"] or job.get("decision") in {"refine", "accept"}
        publish_attempts.append(job["id"])
        if len(publish_attempts) == 1:
            route.fulfill(status=500, json={"error": "Temporary publication failure"})
            return
        published.append(job["id"])
        route.fulfill(json={"slug": "demo-office"})

    page.route("**/api/video-reconstruction/finalize", finalize)
    password = os.environ.get("IMAGEWORLD_ACCESS_PASSWORD")
    if password:
        login = page.request.post(BASE + "/api/auth/session", headers={"x-imageworld-app": "1"}, data={"password": password})
        assert login.ok

    def open_form():
        page.goto(BASE, wait_until="networkidle")
        page.get_by_role("button", name="Build from an image").click()
        expect(page.get_by_role("dialog")).to_be_visible()

    def upload(staged=True):
        page.locator("#file-upload").set_input_files({"name": "room.mp4", "mimeType": "video/mp4", "buffer": b"browser-only-video"})
        page.locator("#world-name").fill("Browser room")
        checkbox = page.get_by_role("checkbox", name="Preview first.", exact=False)
        expect(checkbox).to_be_checked()
        if not staged:
            checkbox.uncheck()
        page.get_by_role("button", name="Generate World", exact=True).click()

    open_form()
    upload()
    expect(page.get_by_role("heading", name="Review your preview")).to_be_visible(timeout=45000)
    expect(page.get_by_alt_text("Recorded view on the left and preview reconstruction on the right")).to_have_js_property("naturalWidth", 4)
    assert len(jobs) == 1 and not published
    page.get_by_role("button", name="Close modal").click()
    page.reload(wait_until="networkidle")
    page.get_by_role("button", name="Build from an image").click()
    expect(page.get_by_role("heading", name="Review your preview")).to_be_visible()
    assert len(jobs) == 1, "Reopening must not upload again"
    page.screenshot(path=str(OUT / "preview-review-desktop.png"), full_page=True)
    page.get_by_role("button", name="Continue refining", exact=True).click()
    expect(page.get_by_role("heading", name="Ready to publish")).to_be_visible(timeout=45000)
    assert len(jobs) == 1 and actions == ["refine"]
    page.get_by_role("button", name="Retry publishing", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=45000)
    assert actions == ["refine"] and len(published) == 1

    page.set_viewport_size({"width": 390, "height": 844})
    open_form()
    upload()
    expect(page.get_by_role("heading", name="Review your preview")).to_be_visible(timeout=45000)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(OUT / "preview-review-mobile.png"), full_page=True)
    page.get_by_role("button", name="Use preview", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=45000)
    assert actions == ["refine", "accept"] and len(published) == 2

    open_form()
    upload()
    expect(page.get_by_role("heading", name="Review your preview")).to_be_visible(timeout=45000)
    page.get_by_role("button", name="Discard", exact=True).click()
    expect(page.get_by_role("heading", name="Review your preview")).to_have_count(0)
    assert len(published) == 2
    assert page.evaluate("localStorage.getItem('imageworld:active-video-job')") is None
    page.get_by_role("button", name="Close modal").click()

    open_form()
    upload(staged=False)
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=45000)
    assert len(published) == 3 and len(jobs) == 4
    assert actions == ["refine", "accept"]
    browser.close()
    print("Preview review/reopen/refine/accept/discard/direct flows passed, including mobile.", flush=True)
