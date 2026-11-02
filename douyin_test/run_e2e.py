from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime

from playwright.sync_api import Locator, Page, Response, TimeoutError as PWTimeout, sync_playwright

import config

# \u4e2a\u4eba\u9875 user-info\uff1a\u7b2c\u4e00\u884c\u662f\u6635\u79f0\uff0c\u5176\u4e2d\u6709\u300c\u6296\u97f3\u53f7\uff1axxx\u300d
PROFILE_INFO_JS = """() => {
  const text = document.querySelector('[data-e2e="user-info"]')?.innerText || '';
  const m = text.match(/\u6296\u97f3\u53f7[\uff1a:]\\s*([A-Za-z0-9_.]+)/);
  return m ? { name: text.split('\\n')[0].trim(), douyin_id: m[1] } : null;
}"""

# \u5f00\u64ad\u65f6\u5934\u50cf\u4e0a\u6709\u300c\u76f4\u64ad\u4e2d\u300d\u94fe\u63a5\uff0c\u6307\u5411 live.douyin.com/<\u76f4\u64ad\u95f4\u53f7>\u3002
# \u5de6\u4fa7\u5bfc\u822a\u680f\u7684\u300c\u76f4\u64ad\u300d\u4e5f\u6307\u5411 live.douyin.com/ \u4f46\u6ca1\u6709\u76f4\u64ad\u95f4\u53f7\uff1bdata-e2e="live-avatar" \u662f\u53f3\u4e0a\u89d2\u81ea\u5df1\u7684\u5934\u50cf\uff0c\u90fd\u4e0d\u80fd\u7528\u3002
LIVE_LINK_JS = """() => {
  const a = [...document.querySelectorAll('a[href*="live.douyin.com/"]')]
    .find(a => /live\\.douyin\\.com\\/\\d+/.test(a.href) && (a.innerText || '').includes('\u76f4\u64ad\u4e2d'));
  return a ? a.href : null;
}"""

# \u524d\u7aef\u628a\u8fde\u7eed\u70b9\u8d5e\u6512\u6210\u4e00\u6279\uff0c\u6700\u540e\u4e00\u6b21\u70b9\u8d5e\u7ea6 2 \u79d2\u540e POST \u6b64\u63a5\u53e3\uff0cquery \u5e26 count=N
LIKE_API = "/webcast/room/like/"
# \u70b9\u8d5e\u7ec4\u4ef6\u89c4\u5219\uff1a\u7b2c\u4e00\u4e0b\u53ea\u8bb0\u65f6\u95f4\uff1b500ms \u5185\u7b2c\u4e8c\u4e0b\u7b97 1 \u8d5e\u5e76\u5f00\u542f\u8fde\u51fb\uff1b\u8fde\u51fb\u5f00\u59cb\u540e 500ms \u5185\u6bcf\u4e0b\u518d\u7b97 1 \u8d5e
# \uff08\u7a97\u53e3\u4e0d\u4f1a\u987a\u5ef6\uff09\uff1b\u7a97\u53e3\u8fc7\u540e\u7684\u7b2c\u4e00\u4e0b\u53ea\u7528\u6765\u9000\u51fa\u8fde\u51fb\u3002200ms \u4e00\u4e0b\u65f6\u6bcf\u8f6e\u300c\u8bb0\u65f6\u95f4\u3001\u8d5e\u3001\u8d5e\u3001\u8d5e\u3001\u9000\u51fa\u300d= 5 \u4e0b 3 \u8d5e\u3002
LIKE_CLICK_INTERVAL_MS = 200
LIKE_READY_JS = """() => performance.getEntriesByType('resource')
  .some(e => e.name.includes('/LiveLike.') && e.name.split('?')[0].endsWith('.js'))"""

# 点「赠送」后页面先 POST 送礼接口（status_code=0 为成功，data.gift_struct.diamond_count 为单价），
# 拿到回复后紧接着请求钱包接口刷新余额（data.diamond），通常 1 秒内都回来
GIFT_SEND_API = "/webcast/gift/send/"
WALLET_API = "/webcast/wallet/info/"

# 直播页进房约 14 秒时自己会请求当前账号信息：登录时 data 里有 nickname、display_id（抖音号）；
# 未登录时 status_code=20003，data.prompts 为「请登录后进入直播间」
USER_ME_API = "/webcast/user/me/"

# 直播画面容器。不要用 page.locator("video").first：送礼后礼物特效插件会临时插入隐藏的 <video>，可能排在直播画面前面
PLAYER_SELECTOR = '[class*="LivePlayer_LivingPlayer"]'
# 点击位置最上层的元素必须在直播画面容器里：点赞层在容器里；礼物特效画布（z-index 40、没设点击穿透）和弹窗不在
CLICK_CLEAR_JS = """([sel, x, y]) => {
  const player = document.querySelector(sel);
  const top = document.elementFromPoint(x, y);
  return !!player && !!top && player.contains(top);
}"""


class Steps:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rows: list[tuple[str, str, str]] = []
        self.started = time.time()

    def add(self, name: str, status: str, detail: str = "") -> None:
        elapsed = time.time() - self.started
        self.rows.append((name, status, detail))
        print(f"[{elapsed:7.2f}s] {status:<4} {name}  {detail}")

    def overall(self) -> tuple[str, int, str]:
        """按最差结果汇总：有 FAIL 为 1，有 WARN 为 2（与「未开播」一致），否则 0；SKIP 不算问题。"""
        fails = sum(1 for _, s, _ in self.rows if s == "FAIL")
        warns = sum(1 for _, s, _ in self.rows if s == "WARN")
        detail = "、".join(f"{n} 项 {s}" for s, n in (("FAIL", fails), ("WARN", warns)) if n) or "全部通过"
        if fails:
            return "FAIL", 1, detail
        if warns:
            return "WARN", 2, detail
        return "PASS", 0, detail

    def render(self) -> str:
        skipped = "（本次跳过）"
        status, _, summary = self.overall()
        lines = [
            "# 抖音直播间互动链路测试报告",
            "",
            f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"- 总体结果：{status}（{summary}）",
            f"- 目标抖音号：{config.TARGET_DOUYIN_ID}",
            f"- 礼物：{self.args.gift} x{config.GIFT_QTY}" + (skipped if self.args.skip_gift else ""),
            f"- 目标点赞数：{self.args.likes}" + (skipped if self.args.skip_like else ""),
            "",
            "| 步骤 | 结果 | 说明 |",
            "|---|---|---|",
        ]
        for name, status, detail in self.rows:
            lines.append(f"| {name} | {status} | {detail.replace('|', '/')} |")
        return "\n".join(lines) + "\n"


def shot(page: Page, name: str) -> None:
    config.SHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(config.SHOT_DIR / f"{name}.png"), full_page=False)


def assert_session(ctx) -> None:
    cookies = ctx.cookies()
    names = {c["name"] for c in cookies if c["domain"].endswith("douyin.com")}
    missing = {"sessionid", "sid_tt"} - names
    if missing:
        raise RuntimeError(f"登录态缺失，请先运行 setup_login.py（缺 {sorted(missing)}）")


def check_account(me: dict, expected_id: str) -> tuple[str, str]:
    """用直播页自己请求的 /webcast/user/me/ 回复核对登录状态和账号；返回（昵称，抖音号）。"""
    data = me.get("data") if isinstance(me.get("data"), dict) else {}
    if me.get("status_code") != 0 or not data.get("display_id"):
        reason = data.get("prompts") or f"status_code={me.get('status_code')}"
        raise RuntimeError(f"登录已失效（{reason}），请重新运行 setup_login.py")
    nickname, douyin_id = data.get("nickname") or "-", str(data["display_id"])
    if douyin_id != expected_id:
        raise RuntimeError(f"当前登录的是 {nickname} / {douyin_id}，不是配置的 MY_DOUYIN_ID {expected_id}，已停止")
    return nickname, douyin_id


def open_target_profile(page: Page, sec_uid: str, douyin_id: str) -> dict:
    """\u76f4\u63a5\u6253\u5f00\u76ee\u6807\u4e2a\u4eba\u9875\uff0c\u6838\u5bf9\u6296\u97f3\u53f7\uff0c\u5e76\u4ece\u300c\u76f4\u64ad\u4e2d\u300d\u94fe\u63a5\u53d6\u76f4\u64ad\u95f4\u53f7\uff1b\u672a\u5f00\u64ad\u65f6 web_rid \u4e3a None\u3002"""
    page.goto(f"{config.BASE_URL}/user/{sec_uid}", wait_until="domcontentloaded")
    try:
        info = page.wait_for_function(PROFILE_INFO_JS, timeout=config.NAV_TIMEOUT_MS).json_value()
    except PWTimeout:
        raise RuntimeError("\u76ee\u6807\u4e2a\u4eba\u9875\u6ca1\u6709\u663e\u793a\u51fa\u7528\u6237\u4fe1\u606f\uff08\u53ef\u80fd\u88ab\u9650\u6d41\u6216\u9875\u9762\u7a7a\u767d\uff09") from None
    if info["douyin_id"] != douyin_id:
        raise RuntimeError(f"\u4e2a\u4eba\u9875\u4e0a\u7684\u6296\u97f3\u53f7\u662f {info['douyin_id']}\uff0c\u4e0d\u662f\u914d\u7f6e\u7684 {douyin_id}\uff0c\u8bf7\u68c0\u67e5 TARGET_SEC_UID")

    # \u300c\u76f4\u64ad\u4e2d\u300d\u53ef\u80fd\u6bd4\u7528\u6237\u4fe1\u606f\u665a\u4e00\u70b9\u51fa\u73b0\uff0c\u591a\u7ed9\u51e0\u79d2\u518d\u5224\u5b9a\u672a\u5f00\u64ad
    try:
        href = page.wait_for_function(LIVE_LINK_JS, timeout=5000).json_value()
    except PWTimeout:
        href = None
        # \u5b9e\u6d4b\u8fde\u5f00\u51e0\u4e2a\u4e3b\u9875\u65f6\uff0c\u5185\u5bb9\u4f1a\u5148\u51fa\u73b0\u518d\u6574\u7247\u53d8\u7a7a\uff1b\u8fd9\u79cd\u60c5\u51b5\u4e0d\u80fd\u5f53\u6210\u672a\u5f00\u64ad
        if not page.evaluate(PROFILE_INFO_JS):
            raise RuntimeError("\u76ee\u6807\u4e2a\u4eba\u9875\u5185\u5bb9\u6d88\u5931\uff08\u53ef\u80fd\u88ab\u9650\u6d41\uff09\uff0c\u65e0\u6cd5\u5224\u65ad\u662f\u5426\u5f00\u64ad") from None
    m = re.search(r"live\.douyin\.com/(\d+)", href or "")
    return {**info, "web_rid": m.group(1) if m else None}


def open_live(page: Page, web_rid: str) -> dict:
    """进直播间并等到后面要用的元素出现；返回页面自己请求的 /webcast/user/me/ 回复，供 check_account 核对。"""
    # 直播页资源有几百个，默认只记 250 条资源计时，晚加载的 LiveLike 脚本会记不进去
    page.add_init_script("performance.setResourceTimingBufferSize(5000)")
    ready = False
    # \u53ea\u7b49\u540e\u9762\u8981\u7528\u7684\u5143\u7d20\uff1a\u89c6\u9891\uff08\u70b9\u8d5e\uff09\u3001\u793c\u7269\u680f\uff08\u9001\u793c\uff09\u3001\u4f59\u989d\u6309\u94ae\uff08\u8bfb\u4f59\u989d\uff09\u3002\u4e0d\u770b\u6807\u9898\uff0c\u4e5f\u4e0d\u56fa\u5b9a\u7b49\u3002
    # \u4f59\u989d\u6309\u94ae\u53ea\u8981\u6c42\u51fa\u73b0\uff1a\u4f59\u989d\u4e3a 0 \u65f6\u5b83\u663e\u793a\u300c\u5145\u503c\u300d\u800c\u4e0d\u662f\u6570\u5b57
    try:
        # 先挂上监听再跳转，账号信息请求无论早晚都不会漏
        with page.expect_response(lambda r: USER_ME_API in r.url, timeout=config.NAV_TIMEOUT_MS) as me_info:
            page.goto(config.LIVE_URL.format(web_rid=web_rid), wait_until="domcontentloaded")
            for selector in (PLAYER_SELECTOR, '[class*="gift_item_gift_bar"]', '[data-e2e="recharge-btn"]'):
                page.wait_for_selector(selector, timeout=config.NAV_TIMEOUT_MS)
            ready = True
    except PWTimeout:
        if not ready:
            raise
        raise RuntimeError(f"进直播间后 {config.NAV_TIMEOUT_MS // 1000} 秒内没等到当前账号信息（{USER_ME_API}）") from None
    return _json(me_info.value)


class LikeNotReady(Exception):
    pass


def clicks_for_likes(n: int) -> int:
    """\u6309 200ms \u8282\u594f\u63a8\u7b97\u8981\u5355\u51fb\u51e0\u4e0b\u624d\u5f97\u5230 n \u4e2a\u8d5e\uff1a\u6bcf\u8f6e 5 \u4e0b 3 \u8d5e\uff0c\u6700\u540e\u4e00\u8f6e\u4e0d\u7528\u70b9\u9000\u51fa\u3002"""
    q, r = divmod(n, 3)
    if r == 0:
        return max(5 * q - 1, 0)
    return 5 * q + r + 1


def _like_count(url: str) -> int:
    m = re.search(r"[?&]count=(\d+)", url)
    return int(m.group(1)) if m else 0


def do_like(page: Page, target: int) -> dict:
    """\u6bcf 200ms \u5355\u51fb\u4e00\u6b21\u76f4\u64ad\u753b\u9762\u70b9\u8d5e\uff0c\u88ab\u906e\u6321\u65f6\u6682\u505c\u3002
    \u8fd4\u56de clicks\uff08\u5b9e\u9645\u5355\u51fb\u6b21\u6570\uff09\u3001accepted\uff08\u670d\u52a1\u5668\u63a5\u53d7\u7684\u8d5e\u6570\uff09\u3001batches\uff08\u6bcf\u4e2a\u70b9\u8d5e\u8bf7\u6c42\u7684\u7ed3\u679c\uff09\u3001
    paused\uff08\u56e0\u906e\u6321\u6682\u505c\u7684\u79d2\u6570\uff09\u3001stopped\uff08\u906e\u6321\u592a\u4e45\u800c\u4e2d\u6b62\u7684\u539f\u56e0\uff0c\u6ca1\u6709\u5219\u4e3a None\uff09\u3002"""
    try:
        page.wait_for_function(LIKE_READY_JS, timeout=20000)
    except PWTimeout:
        raise LikeNotReady("\u70b9\u8d5e\u7ec4\u4ef6 20 \u79d2\u5185\u672a\u52a0\u8f7d\uff0c\u672a\u70b9\u51fb") from None
    # \u6709\u610f\u4fdd\u7559\u7684\u4f59\u91cf\uff1a\u811a\u672c\u52a0\u8f7d\u5b8c\u5230\u7ec4\u4ef6\u6302\u5230\u9875\u9762\u4e0a\u8fd8\u8981\u4e00\u70b9\u65f6\u95f4\uff0c\u800c\u7ec4\u4ef6\u5143\u7d20\u53ea\u6709\u54c8\u5e0c\u7c7b\u540d\uff08PPcGIai7\uff09\uff0c\u6ca1\u6709\u7a33\u5b9a\u6807\u8bb0\u53ef\u7b49
    page.wait_for_timeout(1000)

    player = page.locator(PLAYER_SELECTOR).first
    player.wait_for(state="visible", timeout=config.ACTION_TIMEOUT_MS)
    box = player.bounding_box()
    if not box:
        raise RuntimeError("\u672a\u627e\u5230\u76f4\u64ad\u753b\u9762\u533a\u57df")
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
            # \u6309\u7edd\u5bf9\u65f6\u523b\u6392\u7a0b\uff0c\u907f\u514d\u6bcf\u6b21\u70b9\u51fb\u7684\u8017\u65f6\u7d2f\u52a0\u628a\u95f4\u9694\u62d6\u8fc7 250ms
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
        # \u6700\u540e\u4e00\u6279\u5728\u6700\u540e\u4e00\u6b21\u70b9\u8d5e\u7ea6 2 \u79d2\u540e\u624d\u53d1\uff1b\u8fde\u7eed 4 \u79d2\u6ca1\u6709\u65b0\u8bf7\u6c42\u5c31\u8ba4\u4e3a\u53d1\u5b8c\u4e86
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
            reason = reason or "\u89e6\u53d1\u98ce\u63a7\u9a8c\u8bc1\u7801"
        batches.append({"count": _like_count(resp.url), "status_code": body.get("status_code"), "reason": reason})

    accepted = sum(b["count"] for b in batches if b["status_code"] == 0)
    return {"clicks": done, "accepted": accepted, "batches": batches, "paused": paused, "stopped": stopped}


BALANCE_JS = """() => {
    const el = document.querySelector('[data-e2e="recharge-btn"]');
    if (!el) return null;
    const n = parseInt((el.innerText || '').replace(/\\D/g, ''), 10);
    return Number.isNaN(n) ? null : n;
}"""


def read_balance(page: Page) -> int | None:
    return page.evaluate(BALANCE_JS)


def wait_balance(page: Page, timeout_ms: int = 20000, below: int | None = None) -> int | None:
    """等余额按钮里出现数字（传 below 时等它小于 below）；等不到返回当前读数。"""
    # 包成对象返回，避免余额为 0 时被 wait_for_function 当成「条件不成立」
    js = f"""(below) => {{
        const n = ({BALANCE_JS})();
        return n !== null && (below === null || n < below) ? {{ v: n }} : null;
    }}"""
    try:
        return page.wait_for_function(js, arg=below, timeout=timeout_ms).json_value()["v"]
    except PWTimeout:
        return read_balance(page)


def read_gift_price(tile: Locator) -> int | None:
    # \u793c\u7269\u5757\u4e0b\u65b9\u663e\u793a\u5355\u4ef7\uff0c\u5982\u300c\u68d2\u68d2\u7cd6 9\u94bb\u300d
    m = re.search(r"(\d+)\s*\u94bb", tile.inner_text())
    return int(m.group(1)) if m else None


class InsufficientBalance(Exception):
    pass


def prepare_gift(page: Page, name: str) -> tuple[Locator, int, int]:
    """\u5b9a\u4f4d\u793c\u7269\u5e76\u8bfb\u51fa\u5355\u4ef7\u548c\u4f59\u989d\uff0c\u4e0d\u505a\u4efb\u4f55\u70b9\u51fb\uff1b\u4f59\u989d\u4e0d\u8db3\u65f6\u629b InsufficientBalance\u3002"""
    # 名字必须完全相等：has_text 是包含匹配，「抖音」会选中 10001 钻的「抖音1号」
    tile = page.locator('[class*="gift_item_gift_bar"]').filter(has=page.get_by_text(name, exact=True)).first
    tile.wait_for(state="attached", timeout=config.ACTION_TIMEOUT_MS)
    price = read_gift_price(tile)
    if not price:
        raise RuntimeError(f"\u672a\u80fd\u4ece\u793c\u7269\u5757\u8bfb\u5230\u300c{name}\u300d\u7684\u4ef7\u683c\uff08\u6216\u4e3a\u514d\u8d39\u793c\u7269\uff09\uff0c\u65e0\u6cd5\u7528\u4f59\u989d\u6821\u9a8c\u662f\u5426\u9001\u51fa")
    before = wait_balance(page)
    if before is None:
        raise RuntimeError("\u672a\u80fd\u8bfb\u53d6\u94bb\u77f3\u4f59\u989d\uff0c\u65e0\u6cd5\u5224\u5b9a\u662f\u5426\u9001\u51fa")
    if before < price:
        raise InsufficientBalance(f"\u4f59\u989d\u4e0d\u8db3\uff1a{before} \u94bb < {name}\u5355\u4ef7 {price} \u94bb\uff0c\u672a\u70b9\u51fb\u8d60\u9001")
    return tile, price, before


def _json(resp: Response) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def do_gift(page: Page, name: str, qty: int) -> tuple[int, int, int]:
    if qty != 1:
        raise ValueError("\u5f53\u524d\u811a\u672c\u53ea\u652f\u6301\u5355\u4e2a\u9001\u793c\uff0cqty \u56fa\u5b9a\u4e3a 1")

    tile, price, before = prepare_gift(page, name)
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

    got: dict[str, Response] = {}

    def on_response(resp: Response) -> None:
        if GIFT_SEND_API in resp.url and resp.request.method == "POST":
            got.setdefault("send", resp)
        elif WALLET_API in resp.url and "send" in got:
            got.setdefault("wallet", resp)

    page.on("response", on_response)
    try:
        send.click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "wallet" not in got:
            page.wait_for_timeout(200)
    finally:
        page.remove_listener("response", on_response)
        # 送完礼礼物块会进入连击状态（「连击 空格」按钮和数量框），把鼠标移开，避免后续误触
        page.mouse.move(5, 5)

    if "send" not in got:
        raise RuntimeError("\u70b9\u51fb\u8d60\u9001\u540e 15 \u79d2\u5185\u6ca1\u6709\u6536\u5230\u9001\u793c\u63a5\u53e3\u7684\u56de\u590d\uff0c\u65e0\u6cd5\u786e\u8ba4\u662f\u5426\u9001\u51fa")
    body = _json(got["send"])
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if body.get("status_code") != 0:
        reason = data.get("prompts") or ("\u89e6\u53d1\u98ce\u63a7\u9a8c\u8bc1\u7801" if got["send"].headers.get("bdturing-verify") else "")
        raise RuntimeError(f"\u9001\u793c\u88ab\u670d\u52a1\u5668\u62d2\u7edd\uff1a{reason or 'status_code=' + str(body.get('status_code'))}")
    unit = (data.get("gift_struct") or {}).get("diamond_count")
    if unit is not None and unit != price:
        raise RuntimeError(f"\u670d\u52a1\u5668\u8bb0\u5f55\u7684\u793c\u7269\u5355\u4ef7\u662f {unit} \u94bb\uff0c\u4e0d\u662f\u9875\u9762\u4e0a\u7684 {price} \u94bb\uff0c\u53ef\u80fd\u9001\u9519\u4e86\u793c\u7269")

    wallet = _json(got["wallet"]) if "wallet" in got else {}
    after = (wallet.get("data") or {}).get("diamond") if wallet.get("status_code") == 0 else None
    if after is None:
        # \u6ca1\u7b49\u5230\u94b1\u5305\u63a5\u53e3\u65f6\uff0c\u9000\u56de\u7b49\u9875\u9762\u4e0a\u7684\u4f59\u989d\u53d8\u5c0f
        after = wait_balance(page, timeout_ms=10000, below=before)
    if after is None:
        raise RuntimeError("\u670d\u52a1\u5668\u5df2\u786e\u8ba4\u9001\u51fa\uff0c\u4f46\u8bfb\u4e0d\u5230\u9001\u793c\u540e\u7684\u4f59\u989d\uff0c\u65e0\u6cd5\u6838\u5bf9\u6263\u8d39")

    delta = before - after
    if delta != price:
        hint = "\u53ef\u80fd\u8fdb\u5165\u4e86\u8fde\u51fb" if delta > price else "\u53ef\u80fd\u672a\u9001\u51fa\u6216\u4f59\u989d\u672a\u5237\u65b0"
        raise RuntimeError(f"\u9001\u793c\u6263\u8d39 {delta} \u94bb\uff0c\u671f\u671b {price} \u94bb\uff08{hint}\uff09")
    return delta, before, after


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--likes", type=int, default=config.LIKE_COUNT)
    parser.add_argument("--gift", default=config.GIFT_NAME)
    parser.add_argument("--skip-gift", action="store_true")
    parser.add_argument("--skip-like", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    steps = Steps(args)
    config.SHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.HEADLESS,
            channel=config.BROWSER_CHANNEL,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=str(config.STATE_FILE),
            viewport=config.VIEWPORT,
            locale=config.LOCALE,
        )
        ctx.set_default_timeout(config.ACTION_TIMEOUT_MS)
        ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        page = ctx.new_page()

        try:
            assert_session(ctx)
            steps.add("\u6821\u9a8c\u767b\u5f55\u6001", "PASS", "cookie sessionid \u5b58\u5728\uff08\u9884\u68c0\uff0c\u8fdb\u76f4\u64ad\u95f4\u540e\u4ee5\u63a5\u53e3\u4e3a\u51c6\uff09")

            target = open_target_profile(page, config.TARGET_SEC_UID, config.TARGET_DOUYIN_ID)
            steps.add("\u6253\u5f00\u76ee\u6807\u4e3b\u9875", "PASS", f"{target['name']} / {target['douyin_id']}\uff08\u5df2\u6838\u5bf9\u6296\u97f3\u53f7\uff09")
            shot(page, "02_target")

            if not target["web_rid"]:
                steps.add("\u76f4\u64ad\u72b6\u6001", "WARN", "\u672a\u5f00\u64ad\uff0c\u6d41\u7a0b\u7ec8\u6b62")
                raise SystemExit(2)

            steps.add("\u76f4\u64ad\u72b6\u6001", "PASS", f"\u76f4\u64ad\u95f4\u53f7 {target['web_rid']}")

            me = open_live(page, target["web_rid"])
            steps.add("\u8fdb\u5165\u76f4\u64ad\u95f4", "PASS", page.title())
            shot(page, "03_live")

            # \u9001\u793c\u3001\u70b9\u8d5e\u4e4b\u524d\u6838\u5bf9\u767b\u5f55\u72b6\u6001\u548c\u8d26\u53f7\uff0c\u4e0d\u5bf9\u5c31\u505c\u6b62
            nickname, my_id = check_account(me, config.MY_DOUYIN_ID)
            steps.add("\u786e\u8ba4\u767b\u5f55\u8d26\u53f7", "PASS", f"{nickname} / {my_id}\uff08\u4e0e\u914d\u7f6e\u4e00\u81f4\uff09")

            balance = wait_balance(page)
            steps.add(
                "\u8d26\u6237\u94bb\u77f3\u4f59\u989d",
                "PASS" if balance is not None else "WARN",
                f"\u5f53\u524d {balance} \u94bb" if balance is not None else "\u672a\u8bfb\u53d6\u5230\u4f59\u989d",
            )

            if not args.skip_gift:
                try:
                    delta, before, after = do_gift(page, args.gift, config.GIFT_QTY)
                except InsufficientBalance as exc:
                    steps.add("\u8d60\u9001\u793c\u7269", "WARN", str(exc))
                else:
                    steps.add(
                        "\u8d60\u9001\u793c\u7269",
                        "PASS",
                        f"{args.gift} x{config.GIFT_QTY}\uff0c\u670d\u52a1\u5668\u786e\u8ba4\u9001\u51fa\uff1b\u94bb {before} \u2192 {after}\uff08\u6263 {delta}\uff09",
                    )
                    shot(page, "04_gift")
            else:
                steps.add("\u8d60\u9001\u793c\u7269", "SKIP", "\u6309\u53c2\u6570\u8df3\u8fc7")

            if not args.skip_like:
                try:
                    like = do_like(page, args.likes)
                    clicks, accepted, batches = like["clicks"], like["accepted"], like["batches"]
                except LikeNotReady as exc:
                    steps.add("\u70b9\u8d5e", "WARN", str(exc))
                else:
                    detail = f"\u5355\u51fb {clicks} \u4e0b\uff0c\u76ee\u6807 {args.likes}\uff0c\u670d\u52a1\u5668\u63a5\u53d7 {accepted}\uff08{len(batches)} \u4e2a\u8bf7\u6c42\uff09"
                    rejected = [b for b in batches if b["status_code"] != 0]
                    if rejected:
                        reasons = "\u3001".join(sorted({b["reason"] or f"status_code={b['status_code']}" for b in rejected}))
                        detail += f"\uff1b\u88ab\u62d2 {sum(b['count'] for b in rejected)} \u6b21\uff1a{reasons}"
                    if like["paused"] >= 0.1 and not like["stopped"]:
                        detail += f"；画面被遮挡，暂停 {like['paused']:.1f} 秒"
                    if like["stopped"]:
                        detail += f"；{like['stopped']}"
                    status = "PASS" if accepted > 0 and not rejected and not like["stopped"] else "WARN"
                    steps.add("\u70b9\u8d5e", status, detail)
                    shot(page, "05_like")
            else:
                steps.add("\u70b9\u8d5e", "SKIP", "\u6309\u53c2\u6570\u8df3\u8fc7")

            final_balance = read_balance(page)
            if final_balance is not None:
                steps.add("\u7ed3\u675f\u4f59\u989d", "PASS", f"{final_balance} \u94bb")

        except SystemExit:
            raise
        except Exception as exc:
            steps.add("\u5f02\u5e38\u4e2d\u65ad", "FAIL", f"{type(exc).__name__}: {exc}")
            try:
                shot(page, "99_error")
            except Exception:
                pass
            raise
        finally:
            config.REPORT_FILE.write_text(steps.render(), encoding="utf-8")
            browser.close()
            status, code, summary = steps.overall()
            print(f"总体结果 {status}（{summary}），退出码 {code}")

    return steps.overall()[1]


if __name__ == "__main__":
    raise SystemExit(main())
