import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 每个账号一个 cookie 文件：<昵称>_<抖音号>.json，由 login_qr.py 扫码生成；进直播间后核对登录的抖音号与文件名一致
AUTH_DIR = ROOT / "auth"

# 运行产物。poll.py 每轮通过环境变量 DOUYIN_RUN_DIR 指定一个单独目录（runs/<时间>/），
# 报告、截图、日志都放进去；并行模式的子进程继承环境变量，所以也写到同一个目录
_RUN_DIR = os.environ.get("DOUYIN_RUN_DIR")
if _RUN_DIR:
    SHOT_DIR = Path(_RUN_DIR) / "shots"
    REPORT_FILE = Path(_RUN_DIR) / "report.md"
    LOG_DIR = Path(_RUN_DIR)
else:
    SHOT_DIR = ROOT / "shots"
    REPORT_FILE = ROOT / "report.md"
    LOG_DIR = ROOT / "logs"
RUNS_DIR = ROOT / "runs"  # poll.py 的每轮目录和轮询汇总

BASE_URL = "https://www.douyin.com"
LOGIN_URL = "https://www.douyin.com"
LIVE_URL = "https://live.douyin.com/{web_rid}"

# 目标的抖音号，用来在个人页上核对身份
TARGET_DOUYIN_ID = "37846826470"
# 目标的 sec_uid：浏览器打开对方主页，网址里 /user/ 后面那段。账号永久编号，改昵称或抖音号都不会变
TARGET_SEC_UID = "MS4wLjABAAAAO_YW6MEp_is4jme17yPEP0GLh7_w0qRRx43q9b8w59JfcOzOFqs7i8V-EzthE3fT"
GIFT_NAME = "人气票"
GIFT_QTY = 1
LIKE_COUNT = 50

HEADLESS = False
BROWSER_CHANNEL = "chrome"
VIEWPORT = {"width": 1440, "height": 900}
LOCALE = "zh-CN"
NAV_TIMEOUT_MS = 45000
ACTION_TIMEOUT_MS = 20000
# 网速慢时「继续等待」的轮数，每轮 NAV_TIMEOUT_MS，中间提示一声，不刷新页面。用于：
# 进房后等账号信息接口（等不到停这个账号）、送礼前等礼物栏和钱包余额（等不到只记送礼失败，点赞照常）
WAIT_ROUNDS = 2
