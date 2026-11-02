"""账号 cookie 文件：auth/<昵称>_<抖音号>.json，内容是 Playwright 的 storage_state，由 login_qr.py 生成。

文件名就是账号的唯一记录：昵称方便认人，抖音号用来在进直播间后核对登录的是不是这个号。
昵称里的 _ 和 Windows 文件名不允许的字符都换成 -，所以第一个 _ 之前是昵称、之后是抖音号（抖音号本身可以含 _）。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config

# 判断「已登录」用的两个 cookie
LOGIN_COOKIES = ("sessionid", "sid_tt")

_NICK_BAD = re.compile(r'[\\/:*?"<>|_\x00-\x1f]')
_DOUYIN_ID = re.compile(r"[A-Za-z0-9_.]+")


@dataclass(frozen=True)
class Account:
    nickname: str  # 文件名里的昵称（替换过字符，可能和抖音上显示的略有不同）
    douyin_id: str
    path: Path

    @property
    def label(self) -> str:
        """日志、报告和截图目录里指代这个账号的名字，就是文件名去掉 .json。"""
        return self.path.stem


def file_nickname(nickname: str) -> str:
    name = _NICK_BAD.sub("-", nickname).strip(" .")
    return name[:40] or "未命名"


def account_path(nickname: str, douyin_id: str) -> Path:
    return config.AUTH_DIR / f"{file_nickname(nickname)}_{douyin_id}.json"


def parse_account(path: Path) -> Account | None:
    """文件名不是「昵称_抖音号」格式的（如旧的 state.json）返回 None。"""
    nickname, sep, douyin_id = path.stem.partition("_")
    if not (sep and nickname and _DOUYIN_ID.fullmatch(douyin_id)):
        return None
    return Account(nickname, douyin_id, path)


def list_accounts() -> list[Account]:
    if not config.AUTH_DIR.exists():
        return []
    found = (parse_account(p) for p in sorted(config.AUTH_DIR.glob("*.json")))
    return [a for a in found if a]


def cookie_problem(account: Account) -> str | None:
    """不开浏览器先看 cookie 文件：读不出来、缺登录 cookie 或已过期时返回原因，没问题返回 None。
    只是预检：没过期的 cookie 也可能已在别处被登出，进直播间后以 /webcast/user/me/ 为准。"""
    try:
        state = json.loads(account.path.read_text(encoding="utf-8"))
        cookies = {c["name"]: c for c in state["cookies"] if str(c.get("domain", "")).endswith("douyin.com")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"cookie 文件读不出来（{type(exc).__name__}）"
    for name in LOGIN_COOKIES:
        if name not in cookies:
            return f"缺少登录 cookie {name}"
        expires = cookies[name].get("expires", -1)
        if isinstance(expires, (int, float)) and 0 < expires < time.time():
            return f"登录 cookie {name} 已在 {datetime.fromtimestamp(expires):%Y-%m-%d %H:%M} 过期"
    return None


def relogin_hint(account: Account, reason: str) -> str:
    return (f"【cookie 失效】账号「{account.label}」（抖音号 {account.douyin_id}）：{reason}。"
            f"请运行 python douyin_test/login_qr.py 扫码重新登录这个账号")
