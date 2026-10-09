"""Exercise validation in the running Next.js server without starting generation.

Run after `npm run dev` or `npm run build && npm start`:
    python scripts/verify_api.py [--url http://127.0.0.1:3000]
"""
import argparse

from playwright.sync_api import sync_playwright


def verify_api(base_url: str) -> None:
    headers = {"x-imageworld-app": "1"}
    upload = {"name": "field.txt", "mimeType": "text/plain", "buffer": b"unexpected file field"}
    cases = [
        ("POST", "/api/generate", {"multipart": {"name": upload}}, 400),
        ("POST", "/api/generate", {"multipart": {"name": "Test", "marbleApiKey": upload}}, 400),
        ("POST", "/api/generate", {"multipart": {"name": "Test"}}, 400),
        ("POST", "/api/generate", {"multipart": {"name": "Test", "file": upload}}, 415),
    ]
    for job_id in ["missing", "ABCDEF123456", "123", "000000000000"]:
        for method in ["GET", "DELETE"]:
            cases.append((method, f"/api/generate/jobs/{job_id}", {}, 404))

    failures = []
    with sync_playwright() as playwright:
        request = playwright.request.new_context(base_url=base_url, extra_http_headers=headers)
        try:
            for method, path, options, expected in cases:
                if method == "DELETE" and request.get(path).status != 404:
                    failures.append(f"{path}: refusal check failed; skipped deletion")
                    continue
                response = request.fetch(path, method=method, **options)
                if response.status != expected:
                    failures.append(f"{method} {path}: expected {expected}, got {response.status}")
                elif "application/json" not in response.headers.get("content-type", ""):
                    failures.append(f"{method} {path}: error response is not JSON")
            forbidden = request.post("/api/generate", headers={"x-imageworld-app": ""})
            if forbidden.status != 403:
                failures.append(f"Generation without the app header: got {forbidden.status}")
        finally:
            request.dispose()
    if failures:
        raise AssertionError("\n".join(failures))
    print(f"API checks passed: {len(cases) + 1}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:3000")
    verify_api(parser.parse_args().url.rstrip("/"))
