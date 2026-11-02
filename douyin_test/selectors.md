# 抖音直播间互动链路 · 选择器清单

实测环境：`www.douyin.com`（Chromium 156，1440×900），账号「鸣约」/ `97093434274`，目标 `37846826470`。

## 页面与元素

| 步骤 | 元素 | 定位方式 | 说明 |
|---|---|---|---|
| 登录态预检 | cookie `sessionid`、`sid_tt` | `context.cookies()`，domain 以 `douyin.com` 结尾 | 只是最快的预检：cookie 过期或在别处被登出时它照样通过，以下一行接口为准 |
| 当前账号与登录状态 | 直播页自己请求的 `/webcast/user/me/` | 跳转前挂 `expect_response`；`status_code == 0` 且 `data.display_id` 等于 `MY_DOUYIN_ID` 才继续 | 进房约 14 秒时发出，落在等礼物栏的时间里，不额外耗时。未登录时返回 `status_code 20003`、`data.prompts`「请登录后进入直播间」。原来去自己个人页抠整页文字，既慢（约 10 秒）又从不核对是不是该用的号 |
| 目标主页 | 网址 | `https://www.douyin.com/user/{TARGET_SEC_UID}` | 不再搜索；sec_uid 是账号永久编号，改昵称或抖音号都不变 |
| 目标身份核对 | 主页 `user-info` | `[data-e2e="user-info"]` 里正则 `抖音号[：:]\s*([A-Za-z0-9_.]+)`，与 `TARGET_DOUYIN_ID` 比对 | 不一致就停止，防止 sec_uid 填错 |
| 是否开播 | 头像上的「直播中」链接 | `a[href*="live.douyin.com/"]` 中地址匹配 `live.douyin.com/<数字>` 且文字含「直播中」 | 数字就是直播间号（web_rid）；看到抖音号后再等 5 秒仍没有才判未开播。左侧导航「直播」也指向 `live.douyin.com/` 但没有数字；`data-e2e="live-avatar"` 是右上角自己的头像，都不能用 |
| 直播间就绪 | 后续要用的元素 | 依次等 `[class*="LivePlayer_LivingPlayer"]`、`[class*="gift_item_gift_bar"]` 出现 | 不看标题、不固定等；余额看钱包接口，不等余额按钮。首次进房实测（从开始进入算）：视频 7.2s，标题 12.6s，点赞组件脚本 14.4s，礼物栏 18.2s；同一浏览器再进约 7s |
| 礼物项 | 单个礼物 | `[class*="gift_item_gift_bar"]` + `filter(has=get_by_text("人气票", exact=True))` | 名字必须完全相等；`has_text` 是包含匹配，「抖音」会选中 10001 钻的「抖音1号」 |
| 礼物单价 | 礼物项文字 | 礼物项 `inner_text()` 后正则 `(\d+)\s*钻` | 如「棒棒糖 9钻」；送礼校验要求扣费 = 单价 |
| 赠送按钮 | 礼物项内的「赠送」 | `item.locator('[data-e2e="gift-btn"]')`；悬停后 2 秒内没出现就移开鼠标再悬停，最多 5 次 | 测试标记，平时就在但隐藏，鼠标进入礼物项 150ms 后才显示。礼物栏的「当前选中」一被页面改动（刚加载时会发生），礼物项就取消悬停状态；鼠标不动就不会再有「鼠标进入」，按钮会一直隐藏（实测偶发过一次 20 秒超时） |
| 点赞 | 直播画面容器 | `[class*="LivePlayer_LivingPlayer"]` 的 boundingBox 中心每 200ms 单击一次 | 没有独立的点赞按钮；画面上盖着一层透明点击层，快捷键 Z 等同单击。别用 `page.locator("video").first`：礼物特效插件（GiftEffectPlugin 等）会临时插入隐藏的 `<video>`，送礼后紧接着点赞时会取到它（实测报过「未找到视频区域」） |
| 点赞就绪 | 点赞组件脚本 | `performance` 资源列表里出现 `/LiveLike.*.js` 后再等 1 秒 | 组件懒加载，进房后十几秒才出现，之前的点击没人处理。那 1 秒是有意保留的余量：组件元素只有哈希类名 `PPcGIai7`，没有稳定标记可等 |
| 点赞遮挡 | 画面中心最上层元素 | 每次单击前 `document.elementFromPoint(中心)` 必须在 `[class*="LivePlayer_LivingPlayer"]` 容器内，否则暂停等待（最多 15 秒），后续排程整体顺延 | 礼物特效画在 canvas 上（`GiftEffectPlugin`，容器 `z-index:40`，没设 `pointer-events:none`），播放期间盖住点赞层（`z-index:1`），点击不算赞；特效由服务器推送的送礼消息触发，开始时间不固定。实测送礼后紧接着点赞，一次只出了 8/20 个赞。`TopEffectPlugin` 设了 `pointerEvents:"none"`，不挡 |
| 送礼成功 | 送礼接口 + 钱包接口 | 监听 `POST /webcast/gift/send/`：`status_code == 0` 才算送出，`data.gift_struct.diamond_count` 应等于页面单价；页面随后请求 `/webcast/wallet/info/`，送礼前余额减去这次的 `data.diamond` 必须 = 单价，10 秒内没等到这次回复就 FAIL。点击前余额 < 单价时不点击 | 约 1 秒出结果，失败时 `data.prompts` 有原因。扣费 > 单价多半是进了连击。不用聊天区「送出了 <礼物名>」：任何观众送礼都会出现，和点赞提示是同一个坑 |
| 钻石余额 | 直播页自己请求的 `/webcast/wallet/info/` | 跳转前挂 `page.on("response")` 一直监听（`Wallet`），取最近一次 `status_code == 0` 回复里的 `data.diamond`（整数） | 进房时请求一次，每次拿到送礼回复后再请求一次。11-02 实测（从跳转算）：`/webcast/user/me/` 17.3s，钱包接口 17.9s，礼物栏和余额按钮 19.3s；接口值与按钮数字一致，但按钮数字另有来源（本地把接口回复改成 0 后按钮仍显示原余额）。不再读礼物栏右侧的余额按钮 `[data-e2e="recharge-btn"]`：余额为 0 时它显示「充值」没有数字，原来会空等 20 秒，再把「余额不足」误报成 FAIL；扣费后它也要 3~6 秒才刷新 |
| 点赞成功 | 点赞接口 | 监听 `POST /webcast/room/like/` 的响应，累加 `status_code == 0` 的 query 参数 `count` | 前端攒批，最后一次点赞约 2 秒后才发；聊天区「为主播点赞了」每人每场只提示一次，且任何观众点赞都会出现，不能用来判定 |

## 踩过的坑

1. **test 域扫码登录不可用**：`test.douyin.com` 上调 `/passport/web/get_qrcode/` 返回 `error_code 1069`、`不能用该app扫码`，因为二维码的 `next` 指向 test 域。必须在 `www.douyin.com` 扫码，cookie 域是 `.douyin.com`，子域自动共享。
2. **不再走搜索**：搜索页要 35~51 秒才出结果，首屏是骨架屏，而且是模糊匹配。原来按「卡片里第一个纯数字文本」取抖音号，会把纯数字昵称（如「39032516470」）当成抖音号，有人冒充时会进错直播间。现在用 sec_uid 直接打开目标主页，约 5~13 秒。
3. **主页可能整片变空**：连续打开几个主页时，用户信息会先出现再消失（疑似限流）。判定未开播前要再确认用户信息还在，否则应报错，不能当成未开播。
4. **点赞没有按钮**：PC 直播间 DOM 里没有 like/digg/heart 类名，点赞靠视频区点击。组件规则：第一下只记时间；500ms 内第二下算 1 赞并开启连击；连击开始后 500ms 内每下再算 1 赞（窗口不顺延）；窗口过后的第一下只用来退出连击。所以每轮固定浪费 2 下，200ms 一下时 5 下出 3 赞，快速双击则会多出赞（5 次双击 = 7 赞）。前端把连续点赞攒成一批，用 `POST /webcast/room/like/?count=N` 提交；`status_code` 非 0 时页面提示「您手速太快了，请休息一下」，该接口还可能触发风控验证码（响应头 `bdturing-verify`）。
5. **送礼物是两步**：点一次礼物块只是选中并浮出「赠送」，第二次点「赠送」才真正扣钻。礼物栏另有快捷键 Q/W/E，但它按**位置**分配给前三个礼物（礼物顺序一变就送错），默认关闭，首次使用会弹「确认送出 XX」框；这两个状态存在 localStorage 里，而脚本每次从同一份 `state.json` 启动，所以没用快捷键。
6. **类名是构建哈希**：`saw7uCUW`、`JJwXO946` 之类每次发版都会变；能用的只有 `gift_item_gift_bar`、`webcast-chatroom___list` 这类带语义的，以及 `data-e2e` 测试标记（如 `user-info`、`gift-btn`）。
7. **WAF**：首屏会先返回抖音的 PoW 挑战页（`_wafchallengeid`），必须真实执行 JS 才能进站，所以 `headless=True` 容易卡住，调试期用有头模式。
