from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "auth" / "state.json"
SHOT_DIR = ROOT / "shots"
REPORT_FILE = ROOT / "report.md"

BASE_URL = "https://www.douyin.com"
LOGIN_URL = "https://www.douyin.com"
LIVE_URL = "https://live.douyin.com/{web_rid}"

# 用来送礼和点赞的账号（即 state.json 登录的那个号）的抖音号；进直播间后核对，不一致就停止
MY_DOUYIN_ID = "97093434274"

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
