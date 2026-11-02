from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime

from playwright.sync_api import Locator, Page, Response, TimeoutError as PWTimeout, sync_playwright

import config

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


def open_live(page: Page, web_rid: str) -> dict:
    """进直播间并等到后面要用的元素出现；返回页面自己请求的 /webcast/user/me/ 回复，供 check_account 核对。"""
    # 直播页资源有几百个，默认只记 250 条资源计时，晚加载的 LiveLike 脚本会记不进去
    page.add_init_script("performance.setResourceTimingBufferSize(5000)")
    ready = False
    # 只等后面要用的元素：视频（点赞）、礼物栏（送礼）、余额按钮（读余额）。不看标题，也不固定等。
    # 余额按钮只要求出现：余额为 0 时它显示「充值」而不是数字
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
    # 礼物块下方显示单价，如「棒棒糖 9钻」
    m = re.search(r"(\d+)\s*钻", tile.inner_text())
    return int(m.group(1)) if m else None


class InsufficientBalance(Exception):
    pass


def prepare_gift(page: Page, name: str) -> tuple[Locator, int, int]:
    """定位礼物并读出单价和余额，不做任何点击；余额不足时抛 InsufficientBalance。"""
    # 名字必须完全相等：has_text 是包含匹配，「抖音」会选中 10001 钻的「抖音1号」
    tile = page.locator('[class*="gift_item_gift_bar"]').filter(has=page.get_by_text(name, exact=True)).first
    tile.wait_for(state="attached", timeout=config.ACTION_TIMEOUT_MS)
    price = read_gift_price(tile)
    if not price:
        raise RuntimeError(f"未能从礼物块读到「{name}」的价格（或为免费礼物），无法用余额校验是否送出")
    before = wait_balance(page)
    if before is None:
        raise RuntimeError("未能读取钻石余额，无法判定是否送出")
    if before < price:
        raise InsufficientBalance(f"余额不足：{before} 钻 < {name}单价 {price} 钻，未点击赠送")
    return tile, price, before


def _json(resp: Response) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def do_gift(page: Page, name: str, qty: int) -> tuple[int, int, int]:
    if qty != 1:
        raise ValueError("当前脚本只支持单个送礼，qty 固定为 1")

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
        raise RuntimeError("点击赠送后 15 秒内没有收到送礼接口的回复，无法确认是否送出")
    body = _json(got["send"])
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if body.get("status_code") != 0:
        reason = data.get("prompts") or ("触发风控验证码" if got["send"].headers.get("bdturing-verify") else "")
        raise RuntimeError(f"送礼被服务器拒绝：{reason or 'status_code=' + str(body.get('status_code'))}")
    unit = (data.get("gift_struct") or {}).get("diamond_count")
    if unit is not None and unit != price:
        raise RuntimeError(f"服务器记录的礼物单价是 {unit} 钻，不是页面上的 {price} 钻，可能送错了礼物")

    wallet = _json(got["wallet"]) if "wallet" in got else {}
    after = (wallet.get("data") or {}).get("diamond") if wallet.get("status_code") == 0 else None
    if after is None:
        # 没等到钱包接口时，退回等页面上的余额变小
        after = wait_balance(page, timeout_ms=10000, below=before)
    if after is None:
        raise RuntimeError("服务器已确认送出，但读不到送礼后的余额，无法核对扣费")

    delta = before - after
    if delta != price:
        hint = "可能进入了连击" if delta > price else "可能未送出或余额未刷新"
        raise RuntimeError(f"送礼扣费 {delta} 钻，期望 {price} 钻（{hint}）")
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
    # 先清掉上次的截图：跳过或失败的步骤不会截图，留着旧图容易被当成本次结果
    for old in config.SHOT_DIR.glob("*.png"):
        old.unlink()

    with sync_playwright() as p:
        # 启动浏览器也放在 try 里：缺登录文件、文件损坏、Chrome 启动失败时同样记 FAIL 并重写报告
        browser = page = None
        try:
            if not config.STATE_FILE.exists():
                steps.add("校验登录态", "FAIL", f"未找到 {config.STATE_FILE}，请先运行 setup_login.py")
                raise SystemExit(1)

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

            assert_session(ctx)
            steps.add("校验登录态", "PASS", "cookie sessionid 存在（预检，进直播间后以接口为准）")

            target = open_target_profile(page, config.TARGET_SEC_UID, config.TARGET_DOUYIN_ID)
            steps.add("打开目标主页", "PASS", f"{target['name']} / {target['douyin_id']}（已核对抖音号）")
            shot(page, "02_target")

            if not target["web_rid"]:
                steps.add("直播状态", "WARN", "未开播，流程终止")
                raise SystemExit(2)

            steps.add("直播状态", "PASS", f"直播间号 {target['web_rid']}")

            me = open_live(page, target["web_rid"])
            steps.add("进入直播间", "PASS", page.title())
            shot(page, "03_live")

            # 送礼、点赞之前核对登录状态和账号，不对就停止
            nickname, my_id = check_account(me, config.MY_DOUYIN_ID)
            steps.add("确认登录账号", "PASS", f"{nickname} / {my_id}（与配置一致）")

            balance = wait_balance(page)
            steps.add(
                "账户钻石余额",
                "PASS" if balance is not None else "WARN",
                f"当前 {balance} 钻" if balance is not None else "未读取到余额",
            )

            if not args.skip_gift:
                try:
                    delta, before, after = do_gift(page, args.gift, config.GIFT_QTY)
                except InsufficientBalance as exc:
                    steps.add("赠送礼物", "WARN", str(exc))
                else:
                    steps.add(
                        "赠送礼物",
                        "PASS",
                        f"{args.gift} x{config.GIFT_QTY}，服务器确认送出；钻 {before} → {after}（扣 {delta}）",
                    )
                    shot(page, "04_gift")
            else:
                steps.add("赠送礼物", "SKIP", "按参数跳过")

            if not args.skip_like:
                try:
                    like = do_like(page, args.likes)
                    clicks, accepted, batches = like["clicks"], like["accepted"], like["batches"]
                except LikeNotReady as exc:
                    steps.add("点赞", "WARN", str(exc))
                else:
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
                    shot(page, "05_like")
            else:
                steps.add("点赞", "SKIP", "按参数跳过")

            final_balance = read_balance(page)
            if final_balance is not None:
                steps.add("结束余额", "PASS", f"{final_balance} 钻")

        except SystemExit:
            raise
        except Exception as exc:
            steps.add("异常中断", "FAIL", f"{type(exc).__name__}: {exc}")
            if page is not None:
                try:
                    shot(page, "99_error")
                except Exception:
                    pass
            raise
        finally:
            config.REPORT_FILE.write_text(steps.render(), encoding="utf-8")
            if browser is not None:
                browser.close()
            status, code, summary = steps.overall()
            print(f"总体结果 {status}（{summary}），退出码 {code}")

    return steps.overall()[1]


if __name__ == "__main__":
    raise SystemExit(main())
