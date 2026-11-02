"""抖音直播间互动链路测试（多账号）：目标主页只打开一次取直播间号，然后各账号进直播间 → 核对账号 → 送礼 → 点赞。

账号来自 auth/<昵称>_<抖音号>.json（login_qr.py 生成）。用 --accounts / --all 选账号，不带时弹出编号菜单；
--mode serial 逐个运行（默认），--mode parallel 每个账号一个子进程同时运行。"""
from __future__ import annotations

import argparse
import multiprocessing
import re
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, Response
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

import config
from accounts import LOGIN_COOKIES, Account, cookie_problem, list_accounts, relogin_hint

# 个人页 user-info：第一行是昵称，其中有「抖音号：xxx」
PROFILE_INFO_JS = """() => {
  const text = document.querySelector('[data-e2e="user-info"]')?.innerText || '';
  const m = text.match(/抖音号[：:]\\s*([A-Za-z0-9_.]+)/);
  return m ? { name: text.split('\\n')[0].trim(), douyin_id: m[1] } : null;
}"""

# 开播时头像上有「直播中」链接，指向 live.douyin.com/<直播间号>。
# 左侧导航栏的「直播」也指向 live.douyin.com/ 但没有直播间号；data-e2e="live-avatar" 是右上角自己的头像，都不能用。
LIVE_LINK_JS = """() => {
  const a = [...document.querySelectorAll('a[href*="live.douyin.com/"]')]
    .find(a => /live\\.douyin\\.com\\/\\d+/.test(a.href) && (a.innerText || '').includes('直播中'));
  return a ? a.href : null;
}"""

# 前端把连续点赞攒成一批，最后一次点赞约 2 秒后 POST 此接口，query 带 count=N
LIKE_API = "/webcast/room/like/"
# 点赞组件规则：第一下只记时间；500ms 内第二下算 1 赞并开启连击；连击开始后 500ms 内每下再算 1 赞
# （窗口不会顺延）；窗口过后的第一下只用来退出连击。200ms 一下时每轮「记时间、赞、赞、赞、退出」= 5 下 3 赞。
LIKE_CLICK_INTERVAL_MS = 200
LIKE_READY_JS = """() => performance.getEntriesByType('resource')
  .some(e => e.name.includes('/LiveLike.') && e.name.split('?')[0].endsWith('.js'))"""

# 点「赠送」后页面先 POST 送礼接口（status_code=0 为成功，data.gift_struct.diamond_count 为单价）
GIFT_SEND_API = "/webcast/gift/send/"
# 钱包接口：进房约 18 秒时、以及每次拿到送礼回复后，页面自己会请求一次；data.diamond 是钻石余额（整数）
WALLET_API = "/webcast/wallet/info/"

# 直播页进房约 14 秒时自己会请求当前账号信息：登录时 data 里有 nickname、display_id（抖音号）；
# 未登录时 status_code=20003，data.prompts 为「请登录后进入直播间」
USER_ME_API = "/webcast/user/me/"

# 直播画面容器。不要用 page.locator("video").first：送礼后礼物特效插件会临时插入隐藏的 <video>，可能排在直播画面前面
PLAYER_SELECTOR = '[class*="LivePlayer_LivingPlayer"]'
# 礼物栏里的每个礼物块。网速慢时它可能比画面晚很多才出来，只在送礼那一步等它
GIFT_BAR_SELECTOR = '[class*="gift_item_gift_bar"]'
# 点击位置最上层的元素必须在直播画面容器里：点赞层在容器里；礼物特效画布（z-index 40、没设点击穿透）和弹窗不在
CLICK_CLEAR_JS = """([sel, x, y]) => {
  const player = document.querySelector(sel);
  const top = document.elementFromPoint(x, y);
  return !!player && !!top && player.contains(top);
}"""


Row = tuple[str, str, str]  # （步骤，PASS/WARN/SKIP/FAIL，说明）
Emit = Callable[[str], None]  # 输出一行日志
MODE_NAME = {"serial": "串行", "parallel": "并行"}


class Steps:
    """一组步骤结果；add() 记一行并立即写日志。"""

    def __init__(self, emit: Emit) -> None:
        self.emit = emit
        self.rows: list[Row] = []
        self.started = time.time()

    def add(self, name: str, status: str, detail: str = "") -> None:
        elapsed = time.time() - self.started
        self.rows.append((name, status, detail))
        self.emit(f"[{elapsed:7.2f}s] {status:<4} {name}  {detail}")


def overall(rows: list[Row]) -> tuple[str, int, str]:
    """按最差结果汇总：有 FAIL 为 1，有 WARN 为 2（与「未开播」一致），否则 0；SKIP 不算问题，全是 SKIP 记 SKIP。"""
    fails = sum(1 for _, s, _ in rows if s == "FAIL")
    warns = sum(1 for _, s, _ in rows if s == "WARN")
    detail = "、".join(f"{n} 项 {s}" for s, n in (("FAIL", fails), ("WARN", warns)) if n) or "全部通过"
    if fails:
        return "FAIL", 1, detail
    if warns:
        return "WARN", 2, detail
    if rows and all(s == "SKIP" for _, s, _ in rows):
        return "SKIP", 0, rows[0][2]
    return "PASS", 0, detail


def cell(text: str) -> str:
    # 表格一行不能断开：Playwright 超时信息带多行 Call log，换行连同缩进改成一个空格
    return re.sub(r"\s*\n\s*", " ", text.strip()).replace("|", "/")


def table(rows: list[Row]) -> list[str]:
    return ["| 步骤 | 结果 | 说明 |", "|---|---|---|", *(f"| {n} | {s} | {cell(d)} |" for n, s, d in rows)]


class Log:
    """控制台 + 日志文件，每行带「[谁]」前缀。并行时子进程不直接写，而是把行放进队列由主进程转写。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = path.open("a", encoding="utf-8")
        self.lock = threading.Lock()

    def write(self, who: str, text: str) -> None:
        with self.lock:
            for line in text.rstrip("\n").splitlines() or [""]:
                out = f"[{who}] {line}" if who else line
                print(out, flush=True)
                self.file.write(out + "\n")
            self.file.flush()

    def emitter(self, who: str) -> Emit:
        return lambda text: self.write(who, text)

    def close(self) -> None:
        self.file.close()


def shot(page: Page, folder: Path, name: str, emit: Emit) -> None:
    """截图只是留记录：失败时只在日志里提示一行，不影响任何步骤的结果。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"{name}.png"), full_page=False)
    except Exception as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        emit(f"截图 {name}.png 失败（{type(exc).__name__}: {first_line}），不影响结果")


def clear_shots() -> None:
    """先清掉上次的截图（含各账号子目录）：跳过或失败的步骤不会截图，留着旧图容易被当成本次结果。"""
    if not config.SHOT_DIR.exists():
        return
    for old in config.SHOT_DIR.rglob("*.png"):
        old.unlink()
    for folder in sorted((d for d in config.SHOT_DIR.rglob("*") if d.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()


def launch_browser(p: Playwright, headless: bool = config.HEADLESS) -> Browser:
    return p.chromium.launch(
        headless=headless,
        channel=config.BROWSER_CHANNEL,
        args=[
            "--disable-blink-features=AutomationControlled",
            # 允许有声自动播放：否则直播页会在画面正中弹「因浏览器限制静音，按 P 或点击打开声音」，
            # 它在画面容器里面，遮挡检查拦不住，点赞全点在它上面（实测 33 下 0 赞）
            "--autoplay-policy=no-user-gesture-required",
            # 浏览器整体静音，多开时不会一起出声
            "--mute-audio",
        ],
    )


def new_context(browser: Browser, state: Path | None = None) -> BrowserContext:
    ctx = browser.new_context(
        storage_state=str(state) if state else None,
        viewport=config.VIEWPORT,
        locale=config.LOCALE,
    )
    ctx.set_default_timeout(config.ACTION_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
    return ctx


def close_quietly(browser: Browser | None) -> None:
    if browser is None:
        return
    try:
        browser.close()
    except Exception:
        pass  # 浏览器已经崩溃或被手动关掉


class CookieInvalid(Exception):
    """账号 cookie 失效（缺、过期，或服务器判定未登录）。只停这个账号，日志里写明是哪个号。"""

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(reason)
        self.step = step


def assert_session(ctx: BrowserContext) -> None:
    names = {c["name"] for c in ctx.cookies() if c["domain"].endswith("douyin.com")}
    missing = set(LOGIN_COOKIES) - names
    if missing:
        raise CookieInvalid("校验登录态", f"浏览器里没有登录 cookie（缺 {'、'.join(sorted(missing))}）")


def check_account(me: dict, expected_id: str) -> tuple[str, str]:
    """用直播页自己请求的 /webcast/user/me/ 回复核对登录状态和账号；返回（昵称，抖音号）。"""
    data = me.get("data") if isinstance(me.get("data"), dict) else {}
    if me.get("status_code") != 0 or not data.get("display_id"):
        reason = data.get("prompts") or f"status_code={me.get('status_code')}"
        raise CookieInvalid("确认登录账号", f"服务器判定未登录（{reason}）")
    nickname, douyin_id = data.get("nickname") or "-", str(data["display_id"])
    if douyin_id != expected_id:
        raise RuntimeError(f"cookie 里登录的是 {nickname} / {douyin_id}，不是文件名上的抖音号 {expected_id}，已停止")
    return nickname, douyin_id


def open_target_profile(page: Page, sec_uid: str, douyin_id: str) -> dict:
    """直接打开目标个人页，核对抖音号，并从「直播中」链接取直播间号；未开播时 web_rid 为 None。"""
    page.goto(f"{config.BASE_URL}/user/{sec_uid}", wait_until="domcontentloaded")
    try:
        info = page.wait_for_function(PROFILE_INFO_JS, timeout=config.NAV_TIMEOUT_MS).json_value()
    except PWTimeout:
        raise RuntimeError("目标个人页没有显示出用户信息（可能被限流或页面空白）") from None
    if info["douyin_id"] != douyin_id:
        raise RuntimeError(f"个人页上的抖音号是 {info['douyin_id']}，不是配置的 {douyin_id}，请检查 TARGET_SEC_UID")

    # 「直播中」可能比用户信息晚一点出现，多给几秒再判定未开播
    try:
        href = page.wait_for_function(LIVE_LINK_JS, timeout=5000).json_value()
    except PWTimeout:
        href = None
        # 实测连开几个主页时，内容会先出现再整片变空；这种情况不能当成未开播
        if not page.evaluate(PROFILE_INFO_JS):
            raise RuntimeError("目标个人页内容消失（可能被限流），无法判断是否开播") from None
    m = re.search(r"live\.douyin\.com/(\d+)", href or "")
    return {**info, "web_rid": m.group(1) if m else None}


class Wallet:
    """一直监听页面自己请求的 /webcast/wallet/info/，余额一律以最近一次成功回复的 data.diamond 为准。
    不读礼物栏的余额按钮：余额为 0 时它显示「充值」，读不到数字。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.diamond: int | None = None
        self.seq = 0  # 成功回复的次数，用来判断某个时刻之后有没有新回复
        self.error = ""  # 最近一次失败回复的原因
        page.on("response", self._on_response)

    def _on_response(self, resp: Response) -> None:
        if WALLET_API not in resp.url:
            return
        body = _json(resp)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        if body.get("status_code") == 0 and isinstance(data.get("diamond"), int):
            self.diamond = data["diamond"]
            self.seq += 1
        else:
            self.error = data.get("prompts") or f"status_code={body.get('status_code')}"

    def wait_new(self, seen: int, timeout_ms: int) -> int | None:
        """等到第 seen 次之后的成功回复，返回最新余额；超时返回 None。seen=0 即等任意一次。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while self.seq <= seen and time.monotonic() < deadline:
            self.page.wait_for_timeout(200)
        return self.diamond if self.seq > seen else None


def open_live(page: Page, web_rid: str) -> tuple[dict, Wallet]:
    """进直播间并等到后面要用的元素出现。
    返回页面自己请求的 /webcast/user/me/ 回复（供 check_account 核对），以及跟踪钱包接口的 Wallet。"""
    # 直播页资源有几百个，默认只记 250 条资源计时，晚加载的 LiveLike 脚本会记不进去
    page.add_init_script("performance.setResourceTimingBufferSize(5000)")
    # 先挂上监听再跳转，账号信息和余额的请求无论早晚都不会漏
    wallet = Wallet(page)
    ready = False
    # 只等直播画面（点赞要用）。礼物栏留到送礼那一步再等：网速慢时它可能很久才出来，不能拖累点赞。
    # 不看标题，也不固定等；余额看钱包接口，不等余额按钮
    try:
        with page.expect_response(lambda r: USER_ME_API in r.url, timeout=config.NAV_TIMEOUT_MS) as me_info:
            page.goto(config.LIVE_URL.format(web_rid=web_rid), wait_until="domcontentloaded")
            page.wait_for_selector(PLAYER_SELECTOR, timeout=config.NAV_TIMEOUT_MS)
            ready = True
    except PWTimeout:
        if not ready:
            raise
        raise RuntimeError(f"进直播间后 {config.NAV_TIMEOUT_MS // 1000} 秒内没等到当前账号信息（{USER_ME_API}）") from None
    return _json(me_info.value), wallet


class LikeNotReady(Exception):
    pass


def clicks_for_likes(n: int) -> int:
    """按 200ms 节奏推算要单击几下才得到 n 个赞：每轮 5 下 3 赞，最后一轮不用点退出。"""
    q, r = divmod(n, 3)
    if r == 0:
        return max(5 * q - 1, 0)
    return 5 * q + r + 1


def _like_count(url: str) -> int:
    m = re.search(r"[?&]count=(\d+)", url)
    return int(m.group(1)) if m else 0


def do_like(page: Page, target: int) -> dict:
    """每 200ms 单击一次直播画面点赞，被遮挡时暂停。
    返回 clicks（实际单击次数）、accepted（服务器接受的赞数）、batches（每个点赞请求的结果）、
    paused（因遮挡暂停的秒数）、stopped（遮挡太久而中止的原因，没有则为 None）。"""
    try:
        page.wait_for_function(LIKE_READY_JS, timeout=20000)
    except PWTimeout:
        raise LikeNotReady("点赞组件 20 秒内未加载，未点击") from None
    # 有意保留的余量：脚本加载完到组件挂到页面上还要一点时间，而组件元素只有哈希类名（PPcGIai7），没有稳定标记可等
    page.wait_for_timeout(1000)

    player = page.locator(PLAYER_SELECTOR).first
    player.wait_for(state="visible", timeout=config.ACTION_TIMEOUT_MS)
    box = player.bounding_box()
    if not box:
        raise RuntimeError("未找到直播画面区域")
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    responses: list[Response] = []

    def on_response(resp: Response) -> None:
        if LIKE_API in resp.url and resp.request.method == "POST":
            responses.append(resp)

    clicks = clicks_for_likes(target)
    page.on("response", on_response)
    try:
        done, paused, stopped = 0, 0.0, None
        start = time.monotonic()
        for i in range(clicks):
            # 按绝对时刻排程，避免每次点击的耗时累加把间隔拖过 250ms
            delay = start + i * LIKE_CLICK_INTERVAL_MS / 1000 - time.monotonic()
            if delay > 0:
                page.wait_for_timeout(delay * 1000)
            # 点击前确认画面中心没被挡住；被挡就暂停到遮挡消失，后面的排程整体顺延，避免恢复后连点
            if not page.evaluate(CLICK_CLEAR_JS, [PLAYER_SELECTOR, cx, cy]):
                t = time.monotonic()
                try:
                    page.wait_for_function(CLICK_CLEAR_JS, arg=[PLAYER_SELECTOR, cx, cy], timeout=15000)
                except PWTimeout:
                    stopped = f"画面被遮挡超过 15 秒，点了 {done} 下后停止"
                    break
                finally:
                    waited = time.monotonic() - t
                    paused += waited
                    start += waited
            page.mouse.click(cx, cy)
            done += 1
        # 最后一批在最后一次点赞约 2 秒后才发；连续 4 秒没有新请求就认为发完了
        quiet_since, seen = time.monotonic(), 0
        deadline = quiet_since + 15
        while time.monotonic() < deadline and time.monotonic() - quiet_since < 4:
            page.wait_for_timeout(500)
            if len(responses) != seen:
                seen, quiet_since = len(responses), time.monotonic()
    finally:
        page.remove_listener("response", on_response)

    batches = []
    for resp in responses:
        try:
            body = resp.json()
        except Exception:
            body = {}
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        reason = data.get("prompts") or ""
        if resp.headers.get("bdturing-verify"):
            reason = reason or "触发风控验证码"
        batches.append({"count": _like_count(resp.url), "status_code": body.get("status_code"), "reason": reason})

    accepted = sum(b["count"] for b in batches if b["status_code"] == 0)
    return {"clicks": done, "accepted": accepted, "batches": batches, "paused": paused, "stopped": stopped}


def read_gift_price(tile: Locator) -> int | None:
    # 礼物块下方显示单价，如「棒棒糖 9钻」
    m = re.search(r"(\d+)\s*钻", tile.inner_text())
    return int(m.group(1)) if m else None


class InsufficientBalance(Exception):
    pass


class GiftNotReady(Exception):
    """送礼前要等的东西（礼物栏、钱包接口的余额）等了几轮还没有。属于预期内的慢，只记送礼 FAIL、不打堆栈。"""


def wait_rounds(what: str, wait_once: Callable[[int], bool], emit: Emit) -> None:
    """每轮最多等 NAV_TIMEOUT_MS，没等到就提示一声接着等，共 GIFT_WAIT_ROUNDS 轮；不刷新页面、不重进直播间。"""
    wait_s = config.NAV_TIMEOUT_MS // 1000
    rounds = config.GIFT_WAIT_ROUNDS
    for round_no in range(1, rounds + 1):
        if wait_once(config.NAV_TIMEOUT_MS):
            return
        if round_no < rounds:
            emit(f"{what}等了 {wait_s * round_no} 秒还没有，继续等待（不刷新页面）")
    raise GiftNotReady(f"{what}等了 {wait_s * rounds} 秒仍没有（可能网速慢），未送礼")


def wait_gift_ready(page: Page, wallet: Wallet, emit: Emit) -> None:
    """送礼前等礼物栏出来、钱包接口回来。进房时余额只短等过，网速慢时钱包接口可能还没回来。"""

    def gift_bar_shown(timeout_ms: int) -> bool:
        try:
            page.wait_for_selector(GIFT_BAR_SELECTOR, timeout=timeout_ms)
            return True
        except PWTimeout:
            return False

    wait_rounds("礼物栏", gift_bar_shown, emit)
    if wallet.diamond is None:
        try:
            wait_rounds(f"钱包接口（{WALLET_API}）的余额", lambda ms: wallet.wait_new(0, ms) is not None, emit)
        except GiftNotReady as exc:
            raise GiftNotReady(f"{exc}（最近一次失败：{wallet.error}）" if wallet.error else str(exc)) from None


def prepare_gift(page: Page, wallet: Wallet, name: str) -> tuple[Locator, int, int]:
    """定位礼物并读出单价，余额取钱包接口，不做任何点击；余额不足时抛 InsufficientBalance。"""
    # 名字必须完全相等：has_text 是包含匹配，「抖音」会选中 10001 钻的「抖音1号」
    tile = page.locator(GIFT_BAR_SELECTOR).filter(has=page.get_by_text(name, exact=True)).first
    tile.wait_for(state="attached", timeout=config.ACTION_TIMEOUT_MS)
    price = read_gift_price(tile)
    if not price:
        raise RuntimeError(f"未能从礼物块读到「{name}」的价格（或为免费礼物），无法用余额校验是否送出")
    # wait_gift_ready() 已经等过钱包接口，这里直接取最近一次回复
    before = wallet.diamond
    if before is None:
        raise RuntimeError(f"没拿到钱包接口（{WALLET_API}）的余额，无法核对扣费，未点击赠送")
    if before < price:
        raise InsufficientBalance(f"余额不足：{before} 钻 < {name}单价 {price} 钻，未点击赠送")
    return tile, price, before


def _json(resp: Response) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def do_gift(page: Page, wallet: Wallet, name: str, qty: int) -> tuple[int, int, int]:
    if qty != 1:
        raise ValueError("当前脚本只支持单个送礼，qty 固定为 1")

    tile, price, before = prepare_gift(page, wallet, name)
    # 礼物块收到「鼠标进入」150ms 后才显示赠送按钮；但礼物栏的「当前选中」一被页面改动（刚加载时会发生），
    # 它就取消悬停状态。鼠标不动就不会再有「鼠标进入」，所以按钮没出现时移开鼠标再悬停一次
    send = tile.locator('[data-e2e="gift-btn"]')
    for _ in range(5):
        tile.hover()
        try:
            send.wait_for(state="visible", timeout=2000)
            break
        except PWTimeout:
            page.mouse.move(5, 5)
    else:
        raise RuntimeError(f"试了 5 次悬停，「{name}」的赠送按钮仍未出现，未点击")

    sent: dict = {}

    def on_response(resp: Response) -> None:
        # 记下送礼回复到达时钱包接口已有几次成功回复：在它之后的那次才是送礼后的余额
        if GIFT_SEND_API in resp.url and resp.request.method == "POST" and "resp" not in sent:
            sent.update(resp=resp, seq=wallet.seq)

    page.on("response", on_response)
    try:
        send.click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "resp" not in sent:
            page.wait_for_timeout(200)
    finally:
        page.remove_listener("response", on_response)
        # 送完礼礼物块会进入连击状态（「连击 空格」按钮和数量框），把鼠标移开，避免后续误触
        page.mouse.move(5, 5)

    if "resp" not in sent:
        raise RuntimeError("点击赠送后 15 秒内没有收到送礼接口的回复，无法确认是否送出")
    body = _json(sent["resp"])
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if body.get("status_code") != 0:
        reason = data.get("prompts") or ("触发风控验证码" if sent["resp"].headers.get("bdturing-verify") else "")
        raise RuntimeError(f"送礼被服务器拒绝：{reason or 'status_code=' + str(body.get('status_code'))}")
    unit = (data.get("gift_struct") or {}).get("diamond_count")
    if unit is not None and unit != price:
        raise RuntimeError(f"服务器记录的礼物单价是 {unit} 钻，不是页面上的 {price} 钻，可能送错了礼物")

    # 页面拿到送礼回复后会请求钱包接口刷新余额，通常 1 秒内回来
    after = wallet.wait_new(sent["seq"], timeout_ms=10000)
    if after is None:
        why = f"（最近一次失败：{wallet.error}）" if wallet.error else ""
        raise RuntimeError(f"服务器已确认送出，但 10 秒内没等到钱包接口刷新余额{why}，无法核对扣费")

    delta = before - after
    if delta != price:
        hint = "可能进入了连击" if delta > price else "可能未送出或余额未刷新"
        raise RuntimeError(f"送礼扣费 {delta} 钻，期望 {price} 钻（{hint}）")
    return delta, before, after


def interact(page: Page, wallet: Wallet, args: argparse.Namespace, steps: Steps, folder: Path) -> None:
    """进房并核对过账号之后：读余额 → 送礼 → 点赞 → 结束余额。
    各步互不连累：送礼或点赞出错只记那一步 FAIL，后面照常（用户要求不要级联失败）。"""
    # 只短等：网速慢时钱包接口可能更晚才回来，送礼前 wait_gift_ready() 会再等；不送礼时不拖慢点赞
    balance = wallet.wait_new(0, config.ACTION_TIMEOUT_MS)
    if balance is not None:
        steps.add("账户钻石余额", "PASS", f"当前 {balance} 钻")
    else:
        why = f"：{wallet.error}" if wallet.error else ""
        wait_s = config.ACTION_TIMEOUT_MS // 1000
        steps.add("账户钻石余额", "WARN", f"{wait_s} 秒内没等到钱包接口（{WALLET_API}）的余额{why}")

    if not args.skip_gift:
        try:
            wait_gift_ready(page, wallet, steps.emit)
            delta, before, after = do_gift(page, wallet, args.gift, config.GIFT_QTY)
        except InsufficientBalance as exc:
            steps.add("赠送礼物", "WARN", str(exc))
        except GiftNotReady as exc:
            steps.add("赠送礼物", "FAIL", str(exc))
        except Exception as exc:
            record_error(steps, "赠送礼物", exc, page, folder, "04_gift_error")
            try:
                page.mouse.move(5, 5)  # 离开礼物块，免得悬停或连击状态影响后面点赞
            except Exception:
                pass
        else:
            steps.add(
                "赠送礼物",
                "PASS",
                f"{args.gift} x{config.GIFT_QTY}，服务器确认送出；钻 {before} → {after}（扣 {delta}）",
            )
            shot(page, folder, "04_gift", steps.emit)
    else:
        steps.add("赠送礼物", "SKIP", "按参数跳过")

    if not args.skip_like:
        try:
            like = do_like(page, args.likes)
        except LikeNotReady as exc:
            steps.add("点赞", "WARN", str(exc))
        except Exception as exc:
            record_error(steps, "点赞", exc, page, folder, "05_like_error")
        else:
            clicks, accepted, batches = like["clicks"], like["accepted"], like["batches"]
            detail = f"单击 {clicks} 下，目标 {args.likes}，服务器接受 {accepted}（{len(batches)} 个请求）"
            rejected = [b for b in batches if b["status_code"] != 0]
            if rejected:
                reasons = "、".join(sorted({b["reason"] or f"status_code={b['status_code']}" for b in rejected}))
                detail += f"；被拒 {sum(b['count'] for b in rejected)} 次：{reasons}"
            if like["paused"] >= 0.1 and not like["stopped"]:
                detail += f"；画面被遮挡，暂停 {like['paused']:.1f} 秒"
            if like["stopped"]:
                detail += f"；{like['stopped']}"
            status = "PASS" if accepted > 0 and not rejected and not like["stopped"] else "WARN"
            steps.add("点赞", status, detail)
            shot(page, folder, "05_like", steps.emit)
    else:
        steps.add("点赞", "SKIP", "按参数跳过")

    # 最近一次钱包接口回复：送了礼就是送礼后的余额，否则是进房时的
    if wallet.diamond is not None:
        steps.add("结束余额", "PASS", f"{wallet.diamond} 钻")


def record_error(steps: Steps, name: str, exc: Exception, page: Page | None, folder: Path, shot_name: str) -> None:
    """意外错误：把 name 这一步记 FAIL，完整堆栈写进日志，页面还开着时截图。"""
    steps.add(name, "FAIL", f"{type(exc).__name__}: {exc}")
    steps.emit(traceback.format_exc())
    if page is not None:
        shot(page, folder, shot_name, steps.emit)


def resolve_target(account: Account, emit: Emit) -> tuple[list[Row], str | None]:
    """目标主页只打开一次（借用第一个账号的 cookie）：核对目标抖音号并取直播间号。未开播或出错时直播间号为 None。"""
    steps = Steps(emit)
    web_rid = None
    try:
        with sync_playwright() as p:
            browser = page = None
            try:
                browser = launch_browser(p)
                page = new_context(browser, account.path).new_page()
                target = open_target_profile(page, config.TARGET_SEC_UID, config.TARGET_DOUYIN_ID)
                steps.add(
                    "打开目标主页",
                    "PASS",
                    f"{target['name']} / {target['douyin_id']}（已核对抖音号；借用 {account.label} 的 cookie）",
                )
                shot(page, config.SHOT_DIR, "02_target", emit)
                if target["web_rid"]:
                    steps.add("直播状态", "PASS", f"直播间号 {target['web_rid']}")
                    web_rid = target["web_rid"]
                else:
                    steps.add("直播状态", "WARN", "未开播，流程终止")
            except Exception as exc:
                record_error(steps, "异常中断", exc, page, config.SHOT_DIR, "99_error")
            finally:
                close_quietly(browser)
    except Exception as exc:  # Playwright 自身没能启动
        record_error(steps, "异常中断", exc, None, config.SHOT_DIR, "99_error")
    return steps.rows, web_rid


def run_account(account: Account, web_rid: str, args: argparse.Namespace, emit: Emit) -> dict:
    """一个账号的完整流程。出错只记 FAIL、不往外抛，不影响其他账号；cookie 失效时日志里写明是哪个号。
    返回普通 dict（并行时要跨进程传回主进程）。"""
    steps = Steps(emit)
    folder = config.SHOT_DIR / account.label
    invalid = None
    try:
        with sync_playwright() as p:
            browser = page = None
            try:
                browser = launch_browser(p)
                ctx = new_context(browser, account.path)
                page = ctx.new_page()
                assert_session(ctx)
                steps.add("校验登录态", "PASS", "cookie 齐全（预检，进直播间后以接口为准）")

                me, wallet = open_live(page, web_rid)
                steps.add("进入直播间", "PASS", page.title())
                shot(page, folder, "03_live", emit)

                # 送礼、点赞之前核对登录状态和账号，不对就停止（这是有意的中断，不是级联失败）
                nickname, my_id = check_account(me, account.douyin_id)
                steps.add("确认登录账号", "PASS", f"{nickname} / {my_id}（与文件名一致）")

                interact(page, wallet, args, steps, folder)
            except CookieInvalid as exc:
                invalid = str(exc)
                steps.add(exc.step, "FAIL", f"cookie 已失效：{exc}")
                emit(relogin_hint(account, invalid))
                if page is not None:
                    shot(page, folder, "99_error", emit)
            except Exception as exc:
                record_error(steps, "异常中断", exc, page, folder, "99_error")
            finally:
                close_quietly(browser)
    except Exception as exc:  # Playwright 自身没能启动
        record_error(steps, "异常中断", exc, None, folder, "99_error")
    return {"label": account.label, "rows": steps.rows, "cookie_invalid": invalid}


def _child(account: Account, web_rid: str, args: argparse.Namespace, queue) -> dict:
    """并行模式的子进程入口：日志放进队列，由主进程统一输出和写文件。"""
    return run_account(account, web_rid, args, lambda text: queue.put((account.label, text)))


def _pump(queue, log: Log) -> None:
    while (item := queue.get()) is not None:
        log.write(*item)


def run_parallel(accounts: list[Account], web_rid: str, args: argparse.Namespace, log: Log) -> list[dict]:
    """每个账号一个子进程（各开一个 Chrome）同时运行。"""
    results: dict[str, dict] = {}
    with multiprocessing.Manager() as manager:
        queue = manager.Queue()
        pump = threading.Thread(target=_pump, args=(queue, log), daemon=True)
        pump.start()
        try:
            with ProcessPoolExecutor(max_workers=len(accounts)) as pool:
                futures = {a.label: pool.submit(_child, a, web_rid, args, queue) for a in accounts}
                for label, future in futures.items():
                    try:
                        results[label] = future.result()
                    except Exception as exc:
                        log.write(label, f"子进程异常退出：{type(exc).__name__}: {exc}")
                        rows = [("异常中断", "FAIL", f"子进程异常退出：{type(exc).__name__}: {exc}")]
                        results[label] = {"label": label, "rows": rows, "cookie_invalid": None}
        finally:
            queue.put(None)  # 子进程都结束了，队列里剩下的行写完后让转写线程退出
            pump.join(timeout=10)
    return [results[a.label] for a in accounts if a.label in results]


@dataclass
class Run:
    """一次运行的全部结果，用来写报告。"""

    args: argparse.Namespace
    mode: str
    accounts: list[Account]
    log_path: Path
    target_rows: list[Row] = field(default_factory=list)
    web_rid: str | None = None
    results: list[dict] = field(default_factory=list)

    def account_results(self) -> list[dict]:
        """按选择顺序给出每个账号的结果；没有结果的（运行被中断）补一行 WARN。"""
        done = {r["label"]: r for r in self.results}
        missing = [("运行", "WARN", "未运行（运行被中断）")]
        return [done.get(a.label) or {"label": a.label, "rows": missing, "cookie_invalid": None} for a in self.accounts]

    def summary(self) -> tuple[str, int, str]:
        results = self.account_results()
        status, code, _ = overall(self.target_rows + [row for r in results for row in r["rows"]])
        counts = Counter(overall(r["rows"])[0] for r in results)
        parts = [f"账号 {len(results)} 个：" + "、".join(f"{s} {counts[s]}" for s in ("PASS", "WARN", "FAIL", "SKIP") if counts[s])]
        target_status = overall(self.target_rows)[0] if self.target_rows else None
        if target_status in ("WARN", "FAIL"):
            parts.insert(0, f"目标主页 {target_status}")
        return status, code, "；".join(parts)


def render_report(run: Run) -> str:
    skipped = "（本次跳过）"
    status, _, summary = run.summary()
    lines = [
        "# 抖音直播间互动链路测试报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 总体结果：{status}（{summary}）",
        f"- 运行模式：{MODE_NAME[run.mode]}，共 {len(run.accounts)} 个账号",
        f"- 目标抖音号：{config.TARGET_DOUYIN_ID}" + (f"，直播间号 {run.web_rid}" if run.web_rid else ""),
        f"- 礼物：{run.args.gift} x{config.GIFT_QTY}" + (skipped if run.args.skip_gift else ""),
        f"- 目标点赞数：{run.args.likes}" + (skipped if run.args.skip_like else ""),
        f"- 日志：{run.log_path.relative_to(config.ROOT).as_posix()}",
        "",
        "## 汇总",
        "",
        "| 账号 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    results = run.account_results()
    for r in results:
        status, _, detail = overall(r["rows"])
        # 有问题时列出是哪一步、为什么（原因截短，完整的在下面各账号的明细里）
        bad = [f"{n} {s}：{cell(d)[:50]}" for n, s, d in r["rows"] if s in ("FAIL", "WARN")]
        if r["cookie_invalid"]:
            detail = f"cookie 失效：{r['cookie_invalid']}，需重新扫码登录"
        elif bad:
            detail = "；".join(bad)
        lines.append(f"| {r['label']} | {status} | {cell(detail)} |")
    if run.target_rows:
        lines += ["", "## 目标主页（只打开一次）", "", *table(run.target_rows)]
    for r in results:
        lines += ["", f"## {r['label']}", "", *table(r["rows"])]
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音直播间互动链路测试（多账号）")
    parser.add_argument("--accounts", help="要运行的账号，逗号分隔；可写文件名（昵称_抖音号）、昵称或抖音号")
    parser.add_argument("--all", action="store_true", help="运行 auth/ 里的全部账号")
    parser.add_argument("--mode", choices=("serial", "parallel"), help="serial 逐个运行（默认）；parallel 同时运行")
    parser.add_argument("--likes", type=int, default=config.LIKE_COUNT)
    parser.add_argument("--gift", default=config.GIFT_NAME)
    parser.add_argument("--skip-gift", action="store_true")
    parser.add_argument("--skip-like", action="store_true")
    return parser.parse_args()


class SelectionError(Exception):
    pass


def ask(prompt: str) -> str:
    # Windows 上把输入重定向到 NUL 时 isatty() 也是 True，所以还要处理读到结尾的情况
    try:
        return input(prompt).strip()
    except EOFError:
        raise SelectionError("没有指定账号：请用 --accounts 或 --all（读不到键盘输入，没法用选择菜单）") from None


def pick_accounts(args: argparse.Namespace, available: list[Account]) -> list[Account]:
    if args.all:
        return list(available)
    if not args.accounts:
        return menu_accounts(available)
    chosen: list[Account] = []
    for token in filter(None, (t.strip() for t in re.split(r"[,，]", args.accounts))):
        hits = [a for a in available if token in (a.label, a.nickname, a.douyin_id)]
        if not hits:
            raise SelectionError(f"找不到账号「{token}」")
        if len(hits) > 1:
            raise SelectionError(f"「{token}」对应多个账号（{'、'.join(a.label for a in hits)}），请改用抖音号")
        if hits[0] not in chosen:
            chosen.append(hits[0])
    if not chosen:
        raise SelectionError("--accounts 里没有写账号")
    return chosen


def menu_accounts(available: list[Account]) -> list[Account]:
    if not sys.stdin.isatty():
        raise SelectionError("没有指定账号：请用 --accounts 或 --all（当前不是终端，没法弹出选择菜单）")
    print("可用账号：")
    for i, a in enumerate(available, 1):
        print(f"  {i}. {a.label}")
    while True:
        raw = ask("输入要运行的账号编号，多个用逗号隔开（直接回车 = 全部）：")
        if not raw:
            return list(available)
        try:
            picked = [int(x) for x in re.split(r"[,，\s]+", raw) if x]
        except ValueError:
            picked = []
        if picked and all(1 <= i <= len(available) for i in picked):
            return [available[i - 1] for i in dict.fromkeys(picked)]
        print(f"请输入 1~{len(available)} 之间的编号")


def menu_mode() -> str:
    while True:
        raw = ask("运行模式：1 串行，逐个账号（默认）；2 并行，同时运行。请输入 1 或 2：")
        if raw in ("", "1"):
            return "serial"
        if raw == "2":
            return "parallel"


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    available = list_accounts()
    if not available:
        print(f"{config.AUTH_DIR} 里没有账号文件，请先运行 python douyin_test/login_qr.py 扫码登录")
        return 1
    try:
        accounts = pick_accounts(args, available)
        menu_used = not (args.accounts or args.all)
        mode = args.mode or (menu_mode() if menu_used and len(accounts) > 1 else "serial")
    except SelectionError as exc:
        print(exc)
        print("可用账号：" + "、".join(a.label for a in available))
        return 1

    clear_shots()
    log = Log(config.LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.log")
    run = Run(args, mode, accounts, log.path)
    try:
        log.write("", f"本次 {len(accounts)} 个账号，{MODE_NAME[mode]}运行：{'、'.join(a.label for a in accounts)}")
        # cookie 文件预检：缺或过期的账号不开浏览器，直接记 FAIL 并提示重新扫码
        ready: list[Account] = []
        for acc in accounts:
            problem = cookie_problem(acc)
            if problem:
                log.write(acc.label, relogin_hint(acc, problem))
                rows = [("校验登录态", "FAIL", f"cookie 已失效：{problem}")]
                run.results.append({"label": acc.label, "rows": rows, "cookie_invalid": problem})
            else:
                ready.append(acc)

        if ready:
            run.target_rows, run.web_rid = resolve_target(ready[0], log.emitter("目标"))
            if run.web_rid:
                if mode == "parallel":
                    run.results += run_parallel(ready, run.web_rid, args, log)
                else:
                    for acc in ready:
                        run.results.append(run_account(acc, run.web_rid, args, log.emitter(acc.label)))
            else:
                why = "目标主页出错" if overall(run.target_rows)[0] == "FAIL" else "目标未开播"
                for acc in ready:
                    run.results.append({"label": acc.label, "rows": [("进入直播间", "SKIP", f"{why}，未运行")],
                                        "cookie_invalid": None})
    finally:
        config.REPORT_FILE.write_text(render_report(run), encoding="utf-8")
        invalid = [r["label"] for r in run.results if r["cookie_invalid"]]
        if invalid:
            log.write("", f"【cookie 失效】{len(invalid)} 个账号需要运行 login_qr.py 重新扫码：{'、'.join(invalid)}")
        status, code, summary = run.summary()
        log.write("", f"总体结果 {status}（{summary}），退出码 {code}")
        log.write("", f"报告 {config.REPORT_FILE}；日志 {log.path}")
        log.close()
    return run.summary()[1]


if __name__ == "__main__":
    raise SystemExit(main())
