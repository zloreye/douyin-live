# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 协作约定

请使用中文回答用户。

任务存在多种可行方案时，先列出方案由我选择，不替我做决定。

## 项目概述

基于 Playwright（Python sync API）的抖音 PC 端直播间互动链路端到端测试：预检登录 cookie → 用 sec_uid 直接打开目标主播主页并核对抖音号 → 从「直播中」链接取直播间号 → 进直播间并核对当前登录账号 → 送礼 → 点赞，最后输出 Markdown 报告和截图。代码全部在 `douyin_test/`，没有依赖清单、单元测试和 lint 配置。

## 常用命令

环境：Python 3.12 + playwright 1.62。`config.BROWSER_CHANNEL = "chrome"` 使用本机安装的 Google Chrome，不需要 `playwright install`。

```bash
pip install playwright
```

首次登录或登录态失效时（需要用户本人在弹出的浏览器里扫码，300 秒超时；检测到 `sessionid` + `sid_tt` cookie 后写入 `douyin_test/auth/state.json`）：

```bash
python douyin_test/setup_login.py
```

跑完整链路（默认带 `--skip-gift`，原因见「运行前注意」）：

```bash
python douyin_test/run_e2e.py --skip-gift
```

- 参数：`--likes N`、`--gift <礼物名>`、`--skip-gift`、`--skip-like`，默认值来自 `config.py`。
- 没有单独跑某一步的入口：预检、打开主页、进直播间、核对账号总会执行，送礼/点赞用 `--skip-*` 开关组合。
- 退出码由 `Steps.overall()` 按最差结果汇总：0 全部 PASS/SKIP；2 有 WARN（含目标未开播；`setup_login.py` 超时也返回 2）；缺 `auth/state.json` 时记 FAIL、只打印提示不打堆栈，以 1 退出；其它异常（含 Chrome 启动失败、state.json 损坏）记 FAIL，页面已打开时截 `99_error.png`，然后原样抛出，进程以 1 退出。
- 脚本用 `import config`，从任意目录运行都可以；路径都在 `config.py` 里相对文件解析。
- 能离线做的只有语法检查 `python -m py_compile douyin_test/*.py`，其余改动只能真机运行验证。

## 运行前注意

- `run_e2e.py` 每次运行都会用真实账号进入真实直播间点赞（默认 20 赞），不要在用户没要求时运行。
- **送礼消耗账号真实钻石**。除非用户明确要求送礼，运行 `run_e2e.py` 一律加 `--skip-gift`。
- 点赞过快会被服务端拒绝（页面提示「您手速太快了」）或触发风控验证码（响应头 `bdturing-verify`），不要随意缩短点击间隔或频繁重复运行。
- `auth/state.json` 是真实账号的登录 cookie，`report.md` 和 `shots/` 含昵称、抖音号、钻石余额；都已 gitignore，不要输出 cookie 内容，也不要提交这些文件。
- 只能在 `www.douyin.com` 扫码；`test.douyin.com` 会返回 `error_code 1069`。
- 首屏有 WAF PoW 挑战（`_wafchallengeid`），`HEADLESS=True` 容易卡住，保持有头模式。

## 架构

- `config.py`：两个账号的身份（`MY_DOUYIN_ID` 是送礼点赞用的号；`TARGET_SEC_UID` + `TARGET_DOUYIN_ID` 是目标主播）、礼物名、点赞数、超时、视口、浏览器通道和产物路径。
- `setup_login.py`：一次性人工登录，保存 Playwright `storage_state`。
- `run_e2e.py`：单文件流程。`main()` 依次调用各步骤函数，每步用 `Steps.add(name, status, detail)` 记录 PASS / WARN / SKIP / FAIL 并实时打印；启动浏览器也在 `try` 里，`finally` 中无论成败（包括启动阶段失败）都覆盖写入 `report.md`。截图写到 `shots/`：`02_target`、`03_live`、`04_gift`、`05_like`、`99_error`。每次运行开始时先删掉 `shots/` 里的全部 png，目录里只有本次的截图；跳过或失败的步骤没有对应截图。
- 可预期的业务失败用自定义异常降级为 WARN、不中断流程：`InsufficientBalance`（余额小于单价，不点赠送）、`LikeNotReady`（点赞组件 20 秒内未加载）。其它异常记 FAIL 并重新抛出。

### 两道身份核对

- **目标**：不走搜索（搜索慢且是模糊匹配，纯数字昵称会被误当成抖音号），直接打开 `/user/{TARGET_SEC_UID}`，`[data-e2e="user-info"]` 里解析出的抖音号必须等于 `TARGET_DOUYIN_ID`。直播间号从头像上的「直播中」链接 `live.douyin.com/<数字>` 取。主页会被限流整片变空，判「未开播」前要先确认用户信息还在。
- **自己**：`open_live()` 跳转前挂 `expect_response`，抓直播页自己请求的 `/webcast/user/me/`，`check_account()` 要求 `data.display_id` 等于 `MY_DOUYIN_ID`，否则停止。cookie 预检只用于快速失败，登录是否有效以这个接口为准。

### 成功判定（核心设计）

聊天区的「送出了…」「为主播点赞了」任何观众都会触发，不能用来判定，一律看接口回复：

- **送礼**：两步操作，先悬停礼物块浮出 `[data-e2e="gift-btn"]` 再点它（按钮没出现就移开鼠标重新悬停，最多 5 次）。礼物按名字精确匹配（`exact=True`，否则「抖音」会选中「抖音1号」）。判定监听 `POST /webcast/gift/send/`（`status_code == 0`，`data.gift_struct.diamond_count` 等于页面单价）和随后的 `/webcast/wallet/info/`，扣费必须恰好等于单价，扣多了多半是进了连击。只支持 `GIFT_QTY = 1` 的付费礼物（单价从礼物块文字 `(\d+)\s*钻` 解析）。
- **点赞**：PC 直播间没有点赞按钮，按绝对时刻每 200ms 单击直播画面容器 `[class*="LivePlayer_LivingPlayer"]` 的中心（不能用 `video`：礼物特效会临时插入隐藏的 `<video>`）。前端攒批后 `POST /webcast/room/like/?count=N`，累加 `status_code == 0` 的 `count`。连击规则使每轮 5 下只出 3 赞，`clicks_for_likes()` 据此推算点击次数，**修改 `LIKE_CLICK_INTERVAL_MS` 必须同步修改这个公式**。
  - 就绪：点赞组件 `/LiveLike.*.js` 懒加载（进房后十几秒），点击前要等它出现在 `performance` 资源列表里，所以 `open_live()` 用 `add_init_script` 把 resource timing buffer 扩大到 5000。
  - 遮挡：礼物特效 canvas 会盖住点赞层，被盖住时点击不算赞。每次单击前用 `CLICK_CLEAR_JS`（`elementFromPoint`）确认画面中心最上层元素在画面容器内，否则暂停等待（最多 15 秒），后续排程整体顺延。

### 选择器

`douyin_test/selectors.md` 是选择器清单和踩坑记录（含实测耗时和每条规则的由来），修改定位或判定逻辑前先读，修改后同步更新。只用 `data-e2e` 测试标记和带语义的类名（如 `gift_item_gift_bar`、`LivePlayer_LivingPlayer`，配合 `[class*=...]`）；`saw7uCUW`、`PPcGIai7` 这类构建哈希类名每次发版都会变。抖音号这类文本信息优先用 `innerText` + 正则。

## 其他

- `.playwright-mcp/` 是用 Playwright MCP 探索页面时留下的控制台日志和可访问性快照，不是项目代码（已 gitignore）。
- 源码中的中文（注释、字符串、docstring）直接写 UTF-8 字符，不要写成 `\uXXXX` 转义。
- 在页面里执行的 JS 写在 Python 普通三引号字符串里，JS 正则的反斜杠要写两个（`\\s`、`\\d`）；`wait_balance()` 里拼的是 f-string，JS 花括号要写成 `{{ }}`。
