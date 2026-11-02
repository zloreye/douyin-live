from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 每个账号一个 cookie 文件：<昵称>_<抖音号>.json，由 login_qr.py 扫码生成；进直播间后核对登录的抖音号与文件名一致
AUTH_DIR = ROOT / "auth"
SHOT_DIR = ROOT / "shots"
REPORT_FILE = ROOT / "report.md"
LOG_DIR = ROOT / "logs"

BASE_URL = "https://www.douyin.com"
LOGIN_URL = "https://www.douyin.com"
LIVE_URL = "https://live.douyin.com/{web_rid}"

# 目标的抖音号，用来在个人页上核对身份
TARGET_DOUYIN_ID = "37846826470"
# 目标的 sec_uid：浏览器打开对方主页，网址里 /user/ 后面那段。账号永久编号，改昵称或抖音号都不会变
TARGET_SEC_UID = "MS4wLjABAAAAO_YW6MEp_is4jme17yPEP0GLh7_w0qRRx43q9b8w59JfcOzOFqs7i8V-EzthE3fT"
GIFT_NAME = "人气票"
GIFT_QTY = 1
LIKE_COUNT = 20

HEADLESS = False
BROWSER_CHANNEL = "chrome"
VIEWPORT = {"width": 1440, "height": 900}
LOCALE = "zh-CN"
NAV_TIMEOUT_MS = 45000
ACTION_TIMEOUT_MS = 20000
# 送礼前等礼物栏、等钱包接口余额的轮数，每轮 NAV_TIMEOUT_MS，不刷新页面；等不到只记送礼失败，点赞照常
GIFT_WAIT_ROUNDS = 2
