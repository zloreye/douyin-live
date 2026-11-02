# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 协作约定

请使用中文回答用户

任务存在多种可行方案时，先列出方案由我选择，不替我做决定。

## 项目概述

基于 Playwright（Python sync API）的抖音 PC 端直播间互动链路端到端测试：校验登录态 → 读当前账号抖音号 → 按抖音号搜索目标主播 → 进入直播间 → 送礼 → 点赞，最后输出 Markdown 报告和截图。代码全部在 `douyin_test/`。没有 git、依赖清单、单元测试和 lint 配置。

## 常用命令

环境：Python 3.12 + playwright 1.62。`config.BROWSER_CHANNEL = "chrome"` 使用本机安装的 Google Chrome，不依赖 `playwright install` 下载的 Chromium。

```bash
pip install playwright
```

首次登录或登录态失效时（需要用户本人扫码，300 秒超时；检测到 `sessionid` + `sid_tt` cookie 后写入 `auth/state.json`）：

```bash
python douyin_test/setup_login.py
```

跑完整链路（默认带 `--skip-gift`，原因见下方注意事项）：

```bash
python douyin_test/run_e2e.py --skip-gift
```

- 参数：`--likes N`、`--gift <礼物名>`、`--skip-gift`、`--skip-like`。默认值来自 `config.py`。
- 没有单独跑某一步的入口：登录校验、搜索、进房三步总会执行，送礼/点赞用 `--skip-*` 组合开关。
- 退出码：0 正常结束；2 目标未开播（`setup_login.py` 超时也返回 2）；其它异常会记 FAIL、截 `99_error.png` 后抛出。
- 脚本用 `import config`，从任意目录运行都可以；路径都在 `config.py` 里相对文件解析。

## 运行前注意

- **送礼消耗账号真实钻石**。除非用户明确要求送礼，运行 `run_e2e.py` 一律加 `--skip-gift`。
- 点赞过快会被服务端拒绝（页面提示「您手速太快了」）或触发风控验证码（响应头 `bdturing-verify`），不要随意缩短点击间隔或频繁重复运行。
- `auth/state.json` 是真实账号的登录 cookie，不要输出其内容。
- 只能在 `www.douyin.com` 扫码；`test.douyin.com` 会返回 `error_code 1069`。
- 首屏有 WAF PoW 挑战（`_wafchallengeid`），`HEADLESS=True` 容易卡住，保持有头模式。

## 架构

- `config.py`：所有可调参数（目标抖音号、礼物名、点赞数、超时、视口、浏览器通道）和产物路径。
- `setup_login.py`：一次性人工登录，保存 Playwright `storage_state`。
- `run_e2e.py`：单文件流程。`main()` 依次调用各步骤函数，每步用 `Steps.add(name, status, detail)` 记录 PASS / WARN / SKIP / FAIL 并实时打印；`finally` 中无论成败都覆盖写入 `report.md`。截图按步骤编号写到 `shots/`（`01_profile` … `05_like`、`99_error`）。
- 可预期的业务失败用自定义异常降级为 WARN、不中断流程：`InsufficientBalance`（余额小于单价，不点赠送）、`LikeNotReady`（点赞组件未加载）。其它异常记 FAIL 并重新抛出。

### 成功判定（核心设计）

聊天区的「送出了…」「为主播点赞了」任何观众都会触发，不能用来判定。脚本改用：

- **搜索**：抖音搜索是模糊匹配，`CARD_JS` 在页面内遍历卡片叶子节点解析出 name / douyin_id / live_url，再对抖音号做精确比对。卡片含 `live.douyin.com` 链接即为开播，room_id 从链接中取。
- **送礼**：点击前后读钻石余额（`[data-e2e="recharge-btn"]`），扣费必须恰好等于礼物单价（从礼物块文字 `(\d+)\s*钻` 解析）。扣多了多半是进了连击，扣少了可能未送出或余额未刷新（扣费后约 3~6 秒才刷新）。送礼是两步：鼠标悬停礼物块浮出「赠送」，再点「赠送」。
- **点赞**：PC 直播间没有点赞按钮，靠每 200ms 单击 `video` 中心。前端攒批后 `POST /webcast/room/like/?count=N`，监听响应累加 `status_code == 0` 的 `count`。连击规则使每轮 5 下只出 3 赞，`clicks_for_likes()` 据此推算点击次数，**修改 `LIKE_CLICK_INTERVAL_MS` 必须同步修改这个公式**。点赞组件 `/LiveLike.*.js` 懒加载（进房后十几秒），点击前要等它出现在 `performance` 资源列表里，所以 `open_live()` 用 `add_init_script` 把 resource timing buffer 扩大到 5000。

### 选择器

`douyin_test/selectors.md` 是选择器清单和踩坑记录，修改定位逻辑前先读，修改后同步更新。只用 `data-e2e` 测试标记和带语义的类名（如 `gift_item_gift_bar`，配合 `[class*=...]`）；`saw7uCUW` 这类构建哈希类名每次发版都会变。抖音号这类文本信息优先用 `innerText` + 正则。

## 其他

- `.playwright-mcp/` 是用 Playwright MCP 探索页面时留下的控制台日志和可访问性快照，不是项目代码。
- `run_e2e.py` 中部分中文字符串和注释以字面 `\uXXXX` 转义存储（字符串运行时正常，注释不可读）。用 Edit 修改这些行时要按转义原文匹配。
