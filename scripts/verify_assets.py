"""Verify world files created after the production server starts.

Run against `npm start` to catch Next.js's startup public-file index:
    python scripts/verify_assets.py [--url http://127.0.0.1:3000]
"""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright


def verify_assets(base_url: str) -> None:
    root = Path(__file__).resolve().parents[1] / "public" / "worlds"
    payload = b"runtime world asset: 0123456789"
    # The only files removed are in this newly allocated directory under worlds/.
    with TemporaryDirectory(prefix="validation-assets-", dir=root) as temporary:
        directory = Path(temporary).resolve()
        assert directory.parent == root.resolve()
        output = directory / "output" / "world"
        output.mkdir(parents=True)
        (output / "probe.spz").write_bytes(payload)
        (output / "probe.json").write_text('{"runtime":true}', encoding="utf-8")
        url = f"/worlds/{directory.name}/output/world/probe.spz"
        with sync_playwright() as playwright:
            request = playwright.request.new_context(base_url=base_url)
            try:
                response = request.get(url)
                assert response.status == 200, f"New world file returned HTTP {response.status}"
                assert response.body() == payload
                assert int(response.headers["content-length"]) == len(payload)
                head = request.head(url)
                assert head.status == 200 and head.body() == b""
                assert int(head.headers["content-length"]) == len(payload)
                ranged = request.get(url, headers={"Range": "bytes=3-8"})
                assert ranged.status == 206 and ranged.body() == payload[3:9]
                assert ranged.headers["content-range"] == f"bytes 3-8/{len(payload)}"
                suffix = request.get(url, headers={"Range": "bytes=-4"})
                assert suffix.status == 206 and suffix.body() == payload[-4:]
                for value in ["bytes=9999-", "bytes=9-3", "bytes=-0", "bytes=bad"]:
                    invalid = request.get(url, headers={"Range": value})
                    assert invalid.status == 416, (value, invalid.status)
                cached = request.get(url, headers={"If-None-Match": response.headers["etag"]})
                assert cached.status == 304
                metadata = request.get(url.replace("probe.spz", "probe.json"))
                assert metadata.status == 200 and metadata.json() == {"runtime": True}
                assert metadata.headers["content-type"].startswith("application/json")
                assert request.get(url.replace("probe.spz", "missing.spz")).status == 404
                assert request.get(url.replace("probe.spz", "..%5C..%5C..%5Cpackage.json")).status == 404
            finally:
                request.dispose()
    print("Runtime asset checks passed: file contents, HEAD, ranges, caching, JSON, missing files, traversal")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:3000")
    verify_assets(parser.parse_args().url.rstrip("/"))
