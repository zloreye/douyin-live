"""定时轮询：按固定间隔反复运行 run_e2e.py。每轮单独一个目录 runs/<时间>/，全部保留：
report.md、shots/、run_<时间>.log（run_e2e.py 的日志）、console.log（这一轮终端里的完整输出）。
另写 runs/poll_<启动时间>.md 汇总（每轮一行结果）和 runs/poll_<启动时间>.log（轮询本身的日志）。

用法：
  python douyin_test/poll.py --every 1h --all --skip-gift
  python douyin_test/poll.py --every 30m --start 20:00 --until 23:30 --accounts 鸣约,钟花兰 --mode parallel
  python douyin_test/poll.py --every 2h --times 5 --all

时间参数：
  --every   间隔，如 1h、30m、1h30m，不少于 MIN_INTERVAL_MIN 分钟
  --start   第一轮开始时间，HH:MM 或 "YYYY-MM-DD HH:MM"；HH:MM 今天已过就是明天；不填立即开始
  --until   到这个时间就不再开始新一轮（正在跑的一轮会跑完），格式同上；HH:MM 取第一轮之后最近的那个时刻
  --times   最多跑几轮
其余参数（--accounts / --all / --mode / --likes / --gift / --skip-gift / --skip-like）原样转给每一轮的 run_e2e.py。
不带 --accounts / --all 时，启动时弹一次选账号菜单，之后每轮都用这次选的账号。

每轮按「第一轮时刻 + n × 间隔」开始，不会因为每轮耗时慢慢往后漂；上一轮没跑完时不会同时开下一轮，
等它跑完立即开始，期间错过的其它时刻跳过。Ctrl+C 停止。"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import config
import run_e2e
from accounts import list_accounts

# 最小间隔：点赞过快会被拒绝或触发风控验证码，不要频繁重复运行
MIN_INTERVAL_MIN = 5
RUN_E2E = Path(__file__).resolve().parent / "run_e2e.py"
CHILD_EXIT_WAIT_S = 60  # Ctrl+C 后等这一轮的 run_e2e.py 写完报告再退出


def parse_every(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*", text.lower())
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"间隔写法不对：{text!r}，应写成 1h、30m、1h30m 这样")
    hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def parse_clock(text: str, after: datetime) -> datetime:
    """"YYYY-MM-DD HH:MM" 原样返回；HH:MM 取 after 之后（含）最近的那个时刻。"""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        clock = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise ValueError(f"时间写法不对：{text!r}，应写成 20:00 或 \"2026-11-03 20:00\"") from None
    candidate = datetime.combine(after.date(), clock)
    return candidate if candidate >= after else candidate + timedelta(days=1)


def human(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = [f"{h} 小时" if h else "", f"{m} 分" if m else "", f"{s} 秒" if s and not h else ""]
    return " ".join(p for p in parts if p) or "0 秒"


@dataclass
class Round:
    no: int
    planned: datetime
    started: datetime
    folder: Path
    seconds: float = 0.0
    code: int | None = None
    status: str = "运行中"
    summary: str = ""


class Poller:
    def __init__(self, every: timedelta, first: datetime, until: datetime | None, times: int | None,
                 forward: list[str], started: datetime) -> None:
        self.every, self.first, self.until, self.times, self.forward = every, first, until, times, forward
        self.started = started
        self.rounds: list[Round] = []
        config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.summary_path = config.RUNS_DIR / f"poll_{started:%Y%m%d_%H%M%S}.md"
        self.log = (config.RUNS_DIR / f"poll_{started:%Y%m%d_%H%M%S}.log").open("a", encoding="utf-8")

    def say(self, text: str) -> None:
        line = f"[轮询 {datetime.now():%H:%M:%S}] {text}"
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def stop_rule(self) -> str:
        rules = []
        if self.until:
            rules.append(f"{self.until:%Y-%m-%d %H:%M} 之后不再开始新一轮")
        if self.times:
            rules.append(f"最多 {self.times} 轮")
        return "；".join(rules) or "一直运行，Ctrl+C 停止"

    def run_round(self, rnd: Round) -> None:
        """用子进程跑一轮 run_e2e.py，输出目录通过环境变量 DOUYIN_RUN_DIR 传过去（并行子进程也会继承）。
        这一轮的日志 run_<时间>.log 由 run_e2e.py 写在该目录；另外把终端里的完整输出存成 console.log，
        连日志文件打开前的报错、未捕获的异常也不会丢。"""
        rnd.folder.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "DOUYIN_RUN_DIR": str(rnd.folder),
               "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}  # 管道里也用 UTF-8、逐行输出
        t0 = time.monotonic()
        proc = subprocess.Popen([sys.executable, str(RUN_E2E), *self.forward], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        tee = threading.Thread(target=copy_output, args=(proc, rnd.folder / "console.log"), daemon=True)
        tee.start()
        try:
            rnd.code = proc.wait()
        except KeyboardInterrupt:
            # 子进程和我们在同一个控制台，也收到了 Ctrl+C；等它把这一轮的报告写完（转写线程继续读它的输出）
            try:
                rnd.code = proc.wait(timeout=CHILD_EXIT_WAIT_S)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.kill()
            tee.join(timeout=5)
            rnd.seconds = time.monotonic() - t0
            rnd.status, rnd.summary = "中断", "Ctrl+C 停止了这一轮"
            raise
        tee.join(timeout=5)
        rnd.seconds = time.monotonic() - t0
        rnd.status, rnd.summary = read_result(rnd.folder, rnd.code)

    def next_slot(self, done_slot: int) -> int:
        """下一轮的时刻序号：通常是 done_slot + 1；上一轮跑过了头就取最近一个已过的时刻（立即开始），更早错过的跳过。"""
        passed = int((datetime.now() - self.first) / self.every)
        return max(done_slot + 1, passed)

    def loop(self) -> None:
        slot, skipped = 0, 0
        while True:
            planned = self.first + slot * self.every
            if self.until is not None and planned >= self.until:
                self.say(f"下一轮时间 {planned:%Y-%m-%d %H:%M:%S} 已到结束时间 {self.until:%Y-%m-%d %H:%M}，停止轮询")
                return
            if self.times is not None and len(self.rounds) >= self.times:
                self.say(f"已跑满 {self.times} 轮，停止轮询")
                return
            wait = (planned - datetime.now()).total_seconds()
            if wait > 0:
                self.say(f"第 {len(self.rounds) + 1} 轮将在 {planned:%Y-%m-%d %H:%M:%S} 开始（还有 {human(wait)}）")
                sleep_until(planned)
            elif self.rounds:
                extra = f"，期间错过的 {skipped} 个时间点跳过" if skipped else ""
                self.say(f"上一轮跑完时已过了计划时间 {planned:%H:%M:%S}，立即开始{extra}")
            started = datetime.now()
            rnd = Round(len(self.rounds) + 1, planned, started, config.RUNS_DIR / f"{started:%Y%m%d_%H%M%S}")
            self.rounds.append(rnd)
            self.say(f"===== 第 {rnd.no} 轮开始，输出目录 {rnd.folder} =====")
            self.run_round(rnd)
            self.say(f"===== 第 {rnd.no} 轮结束：{rnd.status}（{rnd.summary}），用时 {human(rnd.seconds)} =====")
            self.write_summary()
            new_slot = self.next_slot(slot)
            skipped, slot = new_slot - slot - 1, new_slot

    def write_summary(self) -> None:
        lines = [
            "# 定时轮询汇总",
            "",
            f"- 启动时间：{self.started:%Y-%m-%d %H:%M:%S}",
            f"- 间隔：每 {human(self.every.total_seconds())}，第一轮 {self.first:%Y-%m-%d %H:%M:%S}",
            f"- 结束条件：{self.stop_rule()}",
            f"- 每轮参数：`{' '.join(self.forward)}`",
            f"- 已跑 {len(self.rounds)} 轮：" + "、".join(f"{s} {n}" for s, n in self.counts() if n),
            "",
            "| 轮次 | 计划时间 | 实际开始 | 用时 | 结果 | 说明 | 报告 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in self.rounds:
            link = f"[{r.folder.name}]({r.folder.name}/report.md)"
            lines.append(f"| {r.no} | {r.planned:%m-%d %H:%M:%S} | {r.started:%m-%d %H:%M:%S} | {human(r.seconds)} "
                         f"| {r.status} | {run_e2e.cell(r.summary)} | {link} |")
        self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def counts(self) -> list[tuple[str, int]]:
        statuses = [r.status for r in self.rounds]
        return [(s, statuses.count(s)) for s in ("PASS", "WARN", "FAIL", "中断", "未知")]

    def exit_code(self) -> int:
        """按最差的一轮：有 FAIL / 中断 / 未知为 1，有 WARN 为 2，否则 0。"""
        statuses = {r.status for r in self.rounds}
        if statuses & {"FAIL", "中断", "未知"}:
            return 1
        return 2 if "WARN" in statuses else 0

    def close(self) -> None:
        self.write_summary()
        self.say(f"共跑了 {len(self.rounds)} 轮；汇总 {self.summary_path}")
        self.log.close()


def copy_output(proc: subprocess.Popen, path: Path) -> None:
    """把子进程的输出逐行显示在终端，同时写进这一轮的 console.log。"""
    with path.open("w", encoding="utf-8") as fh:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()


def sleep_until(target: datetime) -> None:
    # 按墙上时间分段睡：电脑睡眠唤醒后发现时间已过就立即返回；分段也让 Ctrl+C 能及时生效
    while (remaining := (target - datetime.now()).total_seconds()) > 0:
        time.sleep(min(remaining, 30))


def read_result(folder: Path, code: int | None) -> tuple[str, str]:
    """从这一轮的 report.md 读「总体结果」；没有报告（如账号参数写错）时按退出码给个说法。"""
    report = folder / "report.md"
    if report.exists():
        m = re.search(r"^- 总体结果：(\w+)（(.*)）$", report.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1), m.group(2)
    return "未知", f"没有生成报告（退出码 {code}），原因看这一轮的 console.log"


def resolve_forward(rest: list[str]) -> list[str]:
    """检查要转给每一轮的参数；没指定账号时弹一次菜单，把选择换成 --accounts（抖音号）和 --mode。"""
    ns = run_e2e.parse_args(rest)
    available = list_accounts()
    if not available:
        raise run_e2e.SelectionError(f"{config.AUTH_DIR} 里没有账号文件，请先运行 python douyin_test/login_qr.py 扫码登录")
    if ns.accounts or ns.all:
        run_e2e.pick_accounts(ns, available)  # 先检查一遍写法，免得一小时后第一轮才报错
        return rest
    chosen = run_e2e.menu_accounts(available)
    forward = rest + ["--accounts", ",".join(a.douyin_id for a in chosen)]
    if not ns.mode:
        forward += ["--mode", run_e2e.menu_mode() if len(chosen) > 1 else "serial"]
    return forward


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="定时轮询 run_e2e.py；除下面几个时间参数外，其余参数原样转给每一轮的 run_e2e.py",
        allow_abbrev=False,
    )
    parser.add_argument("--every", required=True, type=parse_every, help="间隔，如 1h、30m、1h30m")
    parser.add_argument("--start", help="第一轮开始时间，HH:MM 或 \"YYYY-MM-DD HH:MM\"；不填立即开始")
    parser.add_argument("--until", help="到这个时间就不再开始新一轮，格式同 --start")
    parser.add_argument("--times", type=int, help="最多跑几轮")
    opts, rest = parser.parse_known_args(argv)
    if opts.every < timedelta(minutes=MIN_INTERVAL_MIN):
        parser.error(f"--every 不能少于 {MIN_INTERVAL_MIN} 分钟（点赞过快会触发风控）")
    if opts.times is not None and opts.times < 1:
        parser.error("--times 至少为 1")

    started = datetime.now()
    try:
        first = parse_clock(opts.start, started) if opts.start else started
        until = parse_clock(opts.until, first) if opts.until else None
    except ValueError as exc:
        parser.error(str(exc))
    if until is not None and until <= first:
        parser.error(f"--until（{until:%Y-%m-%d %H:%M}）要晚于第一轮开始时间（{first:%Y-%m-%d %H:%M}）")
    try:
        forward = resolve_forward(rest)
    except run_e2e.SelectionError as exc:
        print(exc)
        return 1

    poller = Poller(opts.every, first, until, opts.times, forward, started)
    poller.say(f"每 {human(opts.every.total_seconds())} 运行一轮；{poller.stop_rule()}；每轮参数：{' '.join(forward)}")
    if "--skip-gift" not in forward:
        poller.say("注意：没有加 --skip-gift，每一轮都会给有余额的账号送礼，扣真实钻石")
    try:
        poller.loop()
    except KeyboardInterrupt:
        poller.say("收到 Ctrl+C，停止轮询")
    finally:
        poller.close()
    return poller.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
