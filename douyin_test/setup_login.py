from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

import config


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel=config.BROWSER_CHANNEL,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(viewport=config.VIEWPORT, locale=config.LOCALE)
        page = ctx.new_page()
        page.goto(config.LOGIN_URL, wait_until="domcontentloaded")

        print("浏览器已打开，请在 www.douyin.com 页面扫码登录（二维码约 2 分钟过期）")
        print("注意：不要在 test.douyin.com 扫码，服务端会返回 error_code 1069 拒绝")

        deadline = time.time() + 300
        while time.time() < deadline:
            names = {c["name"] for c in ctx.cookies() if c["domain"].endswith("douyin.com")}
            if {"sessionid", "sid_tt"} <= names:
                break
            page.wait_for_timeout(1000)
        else:
            print("超时：300 秒内未检测到登录态")
            browser.close()
            return 2

        page.wait_for_timeout(2000)
        ctx.storage_state(path=str(config.STATE_FILE))
        print(f"登录态已保存：{config.STATE_FILE}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
