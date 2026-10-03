"""README 用の画像(resources/*.png)を scripts/images.html から撮影する。

usage: uv run python scripts/images.py [--only header,showcase] [--scale 2]
初回は `uv run playwright install chromium-headless-shell` でブラウザを入れる。
"""

import argparse
import functools
import http.server
import pathlib
import threading

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "resources"


def serve():
    """リポジトリ直下を配信する(file:// だと @font-face が読めないため)"""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(ROOT))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="撮影するカードの id(カンマ区切り)")
    ap.add_argument("--scale", type=float, default=2, help="デバイスピクセル比")
    ap.add_argument("--out", type=pathlib.Path, default=OUT, help="出力先(既定は resources/)")
    args = ap.parse_args()
    srv = serve()
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=args.scale)
        page.goto(f"http://127.0.0.1:{srv.server_port}/scripts/images.html")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        ids = args.only.split(",") if args.only else page.eval_on_selector_all(".card", "els => els.map(e => e.id)")
        for cid in ids:
            dst = args.out / f"{cid}.png"
            page.locator(f"#{cid}").screenshot(path=str(dst), animations="disabled")
            print(dst)
        browser.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
