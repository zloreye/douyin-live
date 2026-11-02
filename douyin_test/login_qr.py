"""扫码登录一个抖音账号，把它的 cookie 存成 auth/<昵称>_<抖音号>.json。

流程：打开 www.douyin.com → 点「登录」→ 确认显示的是二维码（必要时点「扫码登录」）→ 你用抖音 App 扫码并在手机上确认 →
回到终端按回车 → 脚本打开自己的个人页读昵称和抖音号 → 保存 cookie。要在终端里运行，因为需要按回车确认。

同一个抖音号只保留一个文件：改过昵称后重新扫码，会删掉旧昵称的文件。"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

import config
from accounts import LOGIN_COOKIES, account_path, list_accounts
from run_e2e import PROFILE_INFO_JS, close_quietly, launch_browser, new_context

LOGIN_PANEL = "#login-panel-new"  # 点「登录」后弹出的登录框
QR_CODE = "#animate_qrcode_container"  # 扫码区 #douyin_login_comp_scan_code 里的二维码
OPEN_TRIES = 3
OPEN_WAIT_MS = 30000  # 网速慢时登录框和二维码要好一会儿才出来


def show_qr(page: Page) -> bool:
    """点「登录」打开登录框，并确保显示的是二维码；成功返回 True。"""
    login_btn = page.get_by_role("button", name="登录", exact=True).first
    panel = page.locator(LOGIN_PANEL)
    qr = page.locator(QR_CODE)
    for attempt in range(1, OPEN_TRIES + 1):
        try:
            if not panel.is_visible():
                login_btn.click(timeout=config.NAV_TIMEOUT_MS)
            panel.wait_for(state="visible", timeout=OPEN_WAIT_MS)
        except PWTimeout:
            # 实测点「登录」偶尔会让整页刷新而不弹框，重新点
            print(f"第 {attempt} 次：点「登录」后 {OPEN_WAIT_MS // 1000} 秒内没弹出登录框")
            continue
        if not qr.is_visible():
            # 默认布局左边就是二维码；如果显示的是别的登录方式，点「扫码登录」切过去
            tab = panel.get_by_text("扫码登录", exact=True)
            if tab.count():
                tab.first.click()
        try:
            qr.wait_for(state="visible", timeout=OPEN_WAIT_MS)
            return True
        except PWTimeout:
            print(f"第 {attempt} 次：登录框弹出了，但 {OPEN_WAIT_MS // 1000} 秒内没显示二维码")
    return False


def logged_in(ctx: BrowserContext) -> bool:
    names = {c["name"] for c in ctx.cookies() if c["domain"].endswith("douyin.com")}
    return set(LOGIN_COOKIES) <= names


def wait_confirm(ctx: BrowserContext) -> None:
    """等你扫码登录后在终端按回车；按了回车但还没登录上，就提示原因并继续等。"""
    while True:
        input("\n扫码并在手机上确认登录后，回到这里按回车：")
        if logged_in(ctx):
            return
        print("还没检测到登录（缺 sessionid / sid_tt）。二维码约 2 分钟过期，过期了请点浏览器里的二维码刷新后重新扫；"
              "手机上要求验证的话先完成验证。")


def read_self(page: Page) -> dict:
    """打开自己的个人页 /user/self，从 user-info 读昵称（name）和抖音号（douyin_id）。"""
    page.goto(f"{config.BASE_URL}/user/self", wait_until="domcontentloaded")
    try:
        return page.wait_for_function(PROFILE_INFO_JS, timeout=config.NAV_TIMEOUT_MS).json_value()
    except PWTimeout:
        raise RuntimeError(f"{config.NAV_TIMEOUT_MS // 1000} 秒内个人页没显示出昵称和抖音号（网速慢或页面空白）") from None


def save_account(ctx: BrowserContext, info: dict) -> Path:
    """按「昵称_抖音号」保存 cookie；同一抖音号旧昵称的文件在新文件写好后删除。"""
    path = account_path(info["name"], info["douyin_id"])
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    ctx.storage_state(path=str(path))
    for old in list_accounts():
        if old.douyin_id == info["douyin_id"] and old.path != path:
            old.path.unlink()
            print(f"已删除同一账号的旧文件：{old.path.name}")
    return path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not sys.stdin.isatty():
        print("请在终端里运行：扫码后需要按回车确认")
        return 1

    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)  # 要人扫码，始终有头
        try:
            ctx = new_context(browser)
            page = ctx.new_page()
            print("正在打开抖音……")
            page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
            if show_qr(page):
                print("二维码已显示，请用抖音 App 扫码")
            else:
                print("没能自动打开二维码：请在浏览器里点右上角「登录」，选「扫码登录」后扫码")
            wait_confirm(ctx)

            print("已登录，正在读取账号的昵称和抖音号……")
            while True:
                try:
                    info = read_self(page)
                    break
                except RuntimeError as exc:
                    print(exc)
                    if input("按回车重试，输入 q 放弃（放弃后要重新扫码）：").strip().lower() == "q":
                        return 1
            path = save_account(ctx, info)
            print(f"已保存：{info['name']}（抖音号 {info['douyin_id']}）→ {path}")
            return 0
        except KeyboardInterrupt:
            print("\n已取消，没有保存")
            return 1
        except EOFError:
            # Windows 上把输入重定向到 NUL 时 isatty() 也是 True，到这里才发现读不到回车
            print("\n读不到键盘输入：请直接在终端里运行，扫码后按回车确认")
            return 1
        finally:
            close_quietly(browser)


if __name__ == "__main__":
    raise SystemExit(main())
