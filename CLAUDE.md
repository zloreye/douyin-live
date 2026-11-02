# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 协作约定

请使用中文回答用户。

任务存在多种可行方案时，先列出方案由我选择，不替我做决定。

## 项目概述

基于 Playwright（Python sync API）的抖音 PC 端直播间互动链路端到端测试，支持多账号：预检各账号的 cookie 文件 → 用 sec_uid 打开目标主播主页（整次运行只打开一次）核对抖音号、从「直播中」链接取直播间号 → 各账号（串行或并行）进直播间并核对当前登录账号 → 送礼 → 点赞，最后输出一份 Markdown 总报告、按账号分目录的截图和日志文件。代码全部在 `douyin_test/`，没有依赖清单、单元测试和 lint 配置。

## 常用命令

环境：Python 3.12 + playwright 1.62。`config.BROWSER_CHANNEL = "chrome"` 使用本机安装的 Google Chrome，不需要 `playwright install`。

```bash
pip install playwright
```

添加账号或某个账号 cookie 失效时，扫码登录（必须由用户本人在终端运行：脚本自动打开抖音、点「登录」显示二维码，用户扫码后在终端按回车，脚本读出昵称和抖音号，存为 `douyin_test/auth/<昵称>_<抖音号>.json`）：

```bash
python douyin_test/login_qr.py
```

跑完整链路（默认带 `--skip-gift`，原因见「运行前注意」）：

```bash
python douyin_test/run_e2e.py --all --skip-gift
```

- 选账号：`--accounts A,B`（可写文件名、昵称或抖音号）或 `--all`；都不带时列出 `auth/` 里的账号让用户输入编号，选了多个还会问运行模式。非交互环境（包括 Claude 的 Bash 工具）必须带 `--accounts` 或 `--all`。
- 模式：`--mode serial` 逐个账号运行（默认）；`--mode parallel` 每个账号一个子进程、一个 Chrome 同时运行。
- 其它参数：`--likes N`、`--gift <礼物名>`、`--skip-gift`、`--skip-like`，默认值来自 `config.py`，对所有账号相同。
- 没有单独跑某一步的入口：预检、打开主页、进直播间、核对账号总会执行，送礼/点赞用 `--skip-*` 开关组合。
- 退出码按所有账号和目标主页步骤里最差的结果汇总（`overall()`）：0 全部 PASS/SKIP；2 有 WARN（含目标未开播）；1 有 FAIL（含 cookie 失效）。某个账号出错只记 FAIL、把堆栈写进日志，不影响其他账号。`auth/` 里没有账号或 `--accounts` 写错时只打印提示，以 1 退出，不写报告。
- 脚本用 `import config` / `import accounts`，从任意目录运行都可以；路径都在 `config.py` 里相对文件解析。
- 能离线做的只有语法检查 `python -m py_compile douyin_test/*.py`，以及 `accounts.py`、选账号、报告渲染这类不碰浏览器的函数；其余改动只能真机运行验证。

## 运行前注意

- `run_e2e.py` 每次运行都会用真实账号进入真实直播间点赞（每个账号默认 20 赞），不要在用户没要求时运行。
- **送礼消耗账号真实钻石**，多个账号就扣多份。除非用户明确要求送礼，运行 `run_e2e.py` 一律加 `--skip-gift`。
- 点赞过快会被服务端拒绝（页面提示「您手速太快了」）或触发风控验证码（响应头 `bdturing-verify`），不要随意缩短点击间隔或频繁重复运行；并行模式下多个账号同时点赞，风险更高。
- 用户网速较慢：两个账号并行时进房从约 30 秒变成约 50 秒。不要随意缩短超时。
- `auth/*.json` 是真实账号的登录 cookie，`report.md`、`shots/`、`logs/` 含昵称、抖音号、钻石余额；都已 gitignore，不要输出 cookie 内容，也不要提交这些文件。
- 只能在 `www.douyin.com` 扫码；`test.douyin.com` 会返回 `error_code 1069`。
- 首屏有 WAF PoW 挑战（`_wafchallengeid`），`HEADLESS=True` 容易卡住，保持有头模式（`login_qr.py` 始终有头）。

## 架构

- `config.py`：目标主播身份（`TARGET_SEC_UID` + `TARGET_DOUYIN_ID`）、礼物名、点赞数、超时、视口、浏览器通道和产物路径（`AUTH_DIR`、`SHOT_DIR`、`REPORT_FILE`、`LOG_DIR`）。
- `accounts.py`：账号文件的命名、解析和预检。文件名 `<昵称>_<抖音号>.json` 就是账号的唯一记录：昵称里的 `_` 和 Windows 文件名不允许的字符都换成 `-`，所以第一个 `_` 前是昵称、后面是抖音号（抖音号本身可以含 `_`）。不是这个格式的文件（如旧的 `state.json`）不会被当成账号。`cookie_problem()` 不开浏览器先看登录 cookie 有没有、过没过期；`relogin_hint()` 生成写明账号的「【cookie 失效】」提示。
- `login_qr.py`：扫码登录。点「登录」后最多等 30 秒弹框、最多点 3 次（网速慢，且点「登录」偶尔会整页刷新不弹框），仍不行就提示用户手动点；扫码后用户在终端按回车，脚本检查 `sessionid` + `sid_tt`，再打开 `/user/self` 读昵称和抖音号，保存 `storage_state`。同一抖音号只留一个文件，改过昵称会删掉旧文件。
- `run_e2e.py`：`main()` 选账号 → 清空 `shots/` → 打开日志 `logs/run_<时间>.log` → 预检 cookie 文件（失效的直接记 FAIL，不开浏览器）→ `resolve_target()` 借用第一个可用账号的 cookie 打开目标主页（整次只一次）→ 每个账号跑 `run_account()`（串行在本进程，并行用 `ProcessPoolExecutor`）→ `finally` 里无论成败都覆盖写入 `report.md`。
  - `run_account()` 每个账号开自己的 Chrome，依次：校验登录态 → 进直播间 → 核对账号 → `interact()`（余额、送礼、点赞、结束余额）。返回普通 dict（并行时要跨进程传回）。
  - 日志：`Steps.add()` 记一行并通过 `emit` 输出；`Log` 同时写控制台和日志文件，每行带 `[账号]`（目标主页是 `[目标]`）前缀。并行时子进程不直接写，而是把行放进 `multiprocessing.Manager().Queue()`，由主进程的 `_pump` 线程转写。
  - 报告：一份 `report.md`，顶部是每个账号一行的汇总表，下面是目标主页和每个账号的步骤明细；被中断没跑完的账号补一行 WARN「未运行」。
  - 截图：`shots/02_target.png`（目标主页）和 `shots/<账号>/`下的 `03_live`、`04_gift`、`05_like`、`99_error`。送礼、点赞出错时截 `04_gift_error`、`05_like_error`。每次运行先删掉 `shots/` 里全部 png 和空目录；跳过的步骤没有截图，截图本身失败只在日志里提示。
- **不要级联失败**（用户要求）：一步出错只记那一步，不连累后面不相关的步骤。
  - `interact()` 里送礼、点赞各自兜底：任何异常都只把「赠送礼物」或「点赞」记 FAIL（堆栈写日志，截 `04_gift_error` / `05_like_error`），后面照常；送礼出错后先把鼠标移开。
  - 预期内的情况用自定义异常，不打堆栈：`InsufficientBalance`（余额小于单价，不点赠送，WARN）、`GiftNotReady`（礼物栏或钱包余额等了几轮还没有，送礼 FAIL）、`LikeNotReady`（点赞组件 20 秒内未加载，WARN）。
  - `shot()` 自己吞掉异常，截图失败只在日志里提示一行，不影响任何步骤（以前 `02_target` 截图失败会让所有账号都不跑）。
  - 有意保留的中断（不是级联）：cookie 失效（`CookieInvalid`，日志给出「【cookie 失效】账号「…」…请运行 login_qr.py」，结束时汇总所有失效账号）、登录账号和文件名对不上、没等到 `/webcast/user/me/`，这些是送礼点赞前的身份核对；直播画面没出来、目标主页失败也会停（后者让所有账号都不跑）。其它意外异常记「异常中断」FAIL，堆栈写进日志，截 `99_error.png`。

### 两道身份核对

- **目标**：不走搜索（搜索慢且是模糊匹配，纯数字昵称会被误当成抖音号），直接打开 `/user/{TARGET_SEC_UID}`，`[data-e2e="user-info"]` 里解析出的抖音号必须等于 `TARGET_DOUYIN_ID`。直播间号从头像上的「直播中」链接 `live.douyin.com/<数字>` 取。主页会被限流整片变空，判「未开播」前要先确认用户信息还在；多账号时也只打开一次。
- **自己**：`open_live()` 跳转前挂 `expect_response`，抓直播页自己请求的 `/webcast/user/me/`。`status_code != 0` 视为 cookie 失效（`CookieInvalid`）；`check_account()` 要求 `data.display_id` 等于文件名里的抖音号，否则停止这个账号。cookie 文件预检和浏览器里的 cookie 检查只用于快速失败，登录是否有效以这个接口为准。

### 成功判定（核心设计）

聊天区的「送出了…」「为主播点赞了」任何观众都会触发，不能用来判定，一律看接口回复：

- **送礼**：进房时不等礼物栏（`open_live()` 只等直播画面），送礼前 `wait_gift_ready()` 才等礼物栏和钱包余额：每轮 `NAV_TIMEOUT_MS`，共 `config.GIFT_WAIT_ROUNDS` 轮，中间提示「继续等待」，不刷新页面、不重进直播间（用户要求），等不到抛 `GiftNotReady`。之后两步操作，先悬停礼物块浮出 `[data-e2e="gift-btn"]` 再点它（按钮没出现就移开鼠标重新悬停，最多 5 次）。礼物按名字精确匹配（`exact=True`，否则「抖音」会选中「抖音1号」）。判定监听 `POST /webcast/gift/send/`（`status_code == 0`，`data.gift_struct.diamond_count` 等于页面单价）和随后的 `/webcast/wallet/info/`，扣费必须恰好等于单价，扣多了多半是进了连击。只支持 `GIFT_QTY = 1` 的付费礼物（单价从礼物块文字 `(\d+)\s*钻` 解析）。
- **余额**：一律取直播页自己请求的 `/webcast/wallet/info/` 的 `data.diamond`（进房时一次，每次送礼回复后一次）。`open_live()` 跳转前创建 `Wallet` 一直监听，送礼前余额、送礼后余额和「结束余额」都取它最近一次的成功回复。「账户钻石余额」那一步只短等 `ACTION_TIMEOUT_MS`（20 秒，等不到记 WARN，不拖慢点赞）；网速慢时钱包接口可能 35 秒后才回来，所以送礼前 `wait_gift_ready()` 会再等。不读余额按钮 `[data-e2e="recharge-btn"]`：余额为 0 时它显示「充值」，读不到数字。
- **点赞**：PC 直播间没有点赞按钮，按绝对时刻每 200ms 单击直播画面容器 `[class*="LivePlayer_LivingPlayer"]` 的中心（不能用 `video`：礼物特效会临时插入隐藏的 `<video>`）。前端攒批后 `POST /webcast/room/like/?count=N`，累加 `status_code == 0` 的 `count`。连击规则使每轮 5 下只出 3 赞，`clicks_for_likes()` 据此推算点击次数，**修改 `LIKE_CLICK_INTERVAL_MS` 必须同步修改这个公式**。
  - 就绪：点赞组件 `/LiveLike.*.js` 懒加载（进房后十几秒），点击前要等它出现在 `performance` 资源列表里，所以 `open_live()` 用 `add_init_script` 把 resource timing buffer 扩大到 5000。
  - 遮挡：礼物特效 canvas 会盖住点赞层，被盖住时点击不算赞。每次单击前用 `CLICK_CLEAR_JS`（`elementFromPoint`）确认画面中心最上层元素在画面容器内，否则暂停等待（最多 15 秒），后续排程整体顺延。
  - 静音提示：Chrome 拦下有声自动播放时，直播页会在画面正中弹「因浏览器限制静音…」。它在画面容器里面，上面的遮挡检查拦不住，点赞会全点在它上面。所以 `launch_browser()` 加了 `--autoplay-policy=no-user-gesture-required` 和 `--mute-audio`。这个提示只在用户自己终端里出现过，Claude 的运行环境里复现不出来。
  - 并行时窗口互相遮挡也没关系：Playwright 默认带 `--disable-backgrounding-occluded-windows` 等参数，点击走 CDP，不需要窗口在前台。

### 选择器

`douyin_test/selectors.md` 是选择器清单和踩坑记录（含实测耗时和每条规则的由来），修改定位或判定逻辑前先读，修改后同步更新。只用 `data-e2e` 测试标记、带语义的类名（如 `gift_item_gift_bar`、`LivePlayer_LivingPlayer`，配合 `[class*=...]`）和带语义的 id（如登录框的 `#login-panel-new`、`#animate_qrcode_container`）；`saw7uCUW`、`PPcGIai7` 这类构建哈希类名每次发版都会变。抖音号这类文本信息优先用 `innerText` + 正则。

## 其他

- `.playwright-mcp/` 是用 Playwright MCP 探索页面时留下的控制台日志和可访问性快照，不是项目代码（已 gitignore）。
- 源码中的中文（注释、字符串、docstring）直接写 UTF-8 字符，不要写成 `\uXXXX` 转义。
- 在页面里执行的 JS 写在 Python 普通三引号字符串里，JS 正则的反斜杠要写两个（`\\s`、`\\d`）；如果用 f-string 拼 JS，JS 的花括号要写成 `{{ }}`。
- Windows 上把输入重定向到 `NUL` 时 `isatty()` 也是 True，所以读键盘输入的地方都要处理 `EOFError`。
