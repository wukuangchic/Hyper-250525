# Hyperliquid 本地下单工具

一个基于官方 `hyperliquid-python-sdk` 的本地下单工具，支持本地终端、Windows CMD 和手机网页控制台。

日常推荐：

- macOS：双击 `order-terminal.command`，打开后可以直接输入 `BTC buy`、`query`。
- 普通终端：直接使用 `./order BTC buy`。
- Windows：双击 `order-terminal-windows.cmd`。

> 真实下单默认会提交到 Hyperliquid。新命令建议先加 `--dry-run` 预演。

## 飞书历史同步（本地子程序）

`sync_history_to_feishu.py` 使用 `.env` 中的飞书自建应用凭据，把 Hyperliquid 历史增量同步到指定多维表格：

- 交易历史：`tblNp0SCcZkyk5Sf`
- 资金费历史：`tbl3ER99oeYGE2kI`
- 当前仓位：`tblk0aXeGF0TtJ4p`（每次成功获取后清空并重写 `coin`、`szi`、`markPx`）

先预览，再正式同步：

```bash
python3 sync_history_to_feishu.py --dry-run
python3 sync_history_to_feishu.py
# 忽略表内最新时间，保留旧记录并从交易所全量追加
python3 sync_history_to_feishu.py --full-refresh
```

每次启动时先删除两张表中由公式标记为 `唯一值=FALSE` 的记录；资金费表当前通过 API 将公式 FALSE 暴露为空白，程序会自动兼容。随后默认从表内最新 `timestamp_ms` 向前回溯 24 小时并直接追加交易所历史，不再逐条扫描去重，从而容忍时间偏差。程序只写入交易所原始毫秒字段 `timestamp_ms`，日期字段 `time` 由飞书公式生成。两张表分别执行 50,000 条上限保护：若现有记录加本轮新增将超限，会先按 `timestamp_ms` 删除最旧记录；可用 `--max-records` 调整上限。可用 `--overlap-hours 48` 调整回溯窗口。首次同步空表时默认从 `2023-01-01 UTC` 开始，也可通过 `--start 2026-07-01` 指定起点。应用密钥只保存在已被 Git 忽略的 `.env` 中。

交易历史同时写入 `oid`（订单 ID）和 `tid`（成交 ID）。一个订单可能产生多笔部分成交并共享 `oid`，因此需要稳定唯一键时优先使用 `tid`；资金费没有成交 ID，使用 `timestamp_ms + coin` 作为唯一键。

## 快速开始

首次安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```text
account_address=0x主账户地址
secret_key=0x私钥或agent私钥
```

说明：

- `account_address` 必须是主账户地址，不是 agent 地址。
- `secret_key` 可以是主钱包私钥，也可以是已授权的 API wallet / agent 私钥。
- 不要把真实私钥提交进 Git。

## 常用命令

```bash
# 查询账户持仓和挂单
query
order query
order --query

# 持仓表里的 realPnl / result 按账户最近最多 2,000 笔成交计算：
# 已实现盈亏 - 手续费 + 同期资金费 + 交易所当前持仓浮盈亏

# 查询 BTC 行情、文本 K 线、当前持仓和近期成交
BTC

# 预演，不下单
BTC buy --dry-run

# 默认 10 美元看多
BTC buy

# 指定金额
BTC buy 25

# 指定价格
BTC buy 10 --price 75000

# 市价买入 / 卖出，使用 IOC 和滑点保护
BTC buy 10 --market
BTC sell 10 --market
BTC buy 10 --market --slippage 1%

# 只减仓 / 平仓，不允许反手
BTC sell --reduce-only

# 服务器跟踪止损：先按当前 mid 回撤 2% 挂 stop，并写入 server_batch.json 的 trail 任务
BTC sell 50 --trail 2%
# systemd/simple-hyper-trail-worker.timer 可在服务器上每分钟运行一次 trail_worker.py
# query 会在 Server Batch 里展示 trail 任务，并避免和 Open Orders 重复显示
# 取消 trail 会先撤链上 stop 单，再把 server_batch.json 标记为 cancelled
BTC --cancel trail

# 取消 BTC 所有挂单
BTC --cancel

# 取消高于 / 低于当前价的 BTC 挂单
BTC --cancel up
BTC --cancel down

# 取消高于 / 低于指定价格的 BTC 挂单
BTC --cancel up --price 80000
BTC --cancel down --price 75000

# 取消买单 / 卖单 / 止盈 / 止损
BTC --cancel buy
BTC --cancel sell
BTC --cancel tp
BTC --cancel sl

# 取消距今 1 小时 / 天 / 周以上的 BTC 挂单
BTC --cancel hour
BTC --cancel day
BTC --cancel week

# 取消距今 3-5 小时，或 3 小时以上的 BTC 挂单
BTC --cancel hour --range 3 5
BTC --cancel hour --range 3

# 取消指定订单
BTC --cancel 441260592983
```

## 命令结构

下单命令可以理解成 6 段：

```text
coin side amount entry/exec tp/sl reduce-only
```

示例：

```bash
BTC buy 100 --price 68000 --tp 72000 --sl 65000
```

各段含义：

- `coin`：标的，例如 `BTC`、`ETH`、`xyz:SMSN`，以 Hyperliquid `meta` API 返回的名字为准。
- `side`：方向，支持 `buy/sell`，对称单使用 `both`。
- `amount`：美元名义金额，默认 `10`；也可以用 `--total` 表示总金额。
- `entry/exec`：入场或执行方式，例如 `--market`、`--price`、`--offset`、`--stop`、`--take`、`--level`、`--range`、`--for`、`--while`、`--tif`、`--slippage`。
- `tp/sl`：止盈止损，例如 `--tp 2%+0.1% --sl 2%-0.1%`；百分比不写正负号时会按方向自动判断。
- `--reduce-only`：只减仓，不允许反手。

默认行为：

- 默认网络是主网。
- 默认真实提交下单或撤单；加 `--dry-run` 只预演。
- 加 `--explain` 只打印解析后的订单计划，不提交，也不计算账户指标。
- 不填价格时，限价单默认按同向订单簿第 `10` 档挂单，且默认 `ALO`。
- `--market` 会按当前 mid 计算数量，并用带滑点保护的 IOC 单成交。
- 真实下单前会尝试把当前合约 cross 杠杆设置为 `maxLeverage`；如果标的不支持 cross，会自动切到 isolated，默认 `5x`。
- 如果数量 round 后名义价值低于 `10` 美元，会向上补一个数量步进。
- 前台只显示核心结果，完整日志写入 `logs/`。

## 对称单

对称单一次提交两个普通限价单：基准价下方挂买单，基准价上方挂卖单，两个方向金额相同。

```bash
# 用当前 mid 做中心：跌 2% 买，涨 2% 卖
BTC both 100 --offset 2%

# 指定 75000 做中心：73500 买，76500 卖
BTC both 100 --price 75000 --offset 2%

# 总金额 200，自动拆成买卖各 100
BTC both --total 200 --offset 2% --explain

# 对称单也可以给两边各自带 TP/SL
JPY both 20 --offset 2% --tp 1% --sl 0.7%
```

注意：

- `amount` 是每边金额；`--total` 会平均拆成买卖两边。
- `--price` 是中心价；不写时使用当前 mid。
- `--offset` 支持百分比或绝对价差，例如 `2%`、`1500`。
- 实际名义金额会按交易所数量精度和当前 mid 的最小价值校验向上取整，可能略高于输入值。
- 对称单只做普通限价单，可以带 `--tp` / `--sl`；买腿按多单方向计算，卖腿按空单方向计算。
- 对称单不能和 `--market`、`--stop`、`--take`、`--for`、`--while`、`--range`、`--scale` 混用。

## 服务器真实网格单

`grid` 现在是有限订单链，不再维持固定数量或固定密度。新建 grid 初始为空，不提交任何格子单；只有仓位 value 超出 `--limit MIN MAX` 后，P4 `limit-chase` 的 IOC 市价回归单确认成交，才会按实际 `avgPx` 在反方向 `2 * gap` 处出生第一张 `grid_leg=1` GTC 单。后续成交由 P2 按 `1 ↔ 0` 延续。

`grid_leg=1` 表示当前往复尚未闭合，是必须继续处理的“链债务”；`grid_leg=0` 表示上一组往复已经闭合，在 withdrawable 紧张时可以终结。P0/P4 的 IOC 市价单是无格属性的出生事件，只有成交后派生的反向限价单带格属性。

### 创建

```bash
BTC grid --limit -300 300 --gap 0.5% --trend 10%
BTC grid --limit 0 300 --gap 0.5% --min 20
BTC grid --limit 200 300 --gap 0.5%
BTC grid --limit 100 400 --avg 200
BTC grid --limit -300 0
```

仓位限制统一使用 `--limit MIN MAX`，按 signed 仓位价值表示范围：空仓为负数，多仓为正数。

- `--limit -200 400`：仓位下限为空仓 200，上限为多仓 400。
- `--limit 200 400`：永远保持多仓，范围为 200–400；低于 200 时不再减仓，高于 400 时不再加仓。
- `--limit -400 0`：永远保持空仓，最多空仓 400。
- `--limit -300 300`：多空都可以开，最大绝对仓位价值 300。

参数：

- `--gap`：P2 往复使用 `1 * gap`，P0/P4 出生反向单使用 `2 * gap`。创建时不生成初始挂单。
- 不写 `--gap`，或写 `--gap 0` / `--gap 0%` 时，默认使用 `最小价格变动百分比 + 折扣后 takerFee + 折扣后 makerFee`。
- `--trend`：数量倾向，默认 `0`；正数让买入数量大于卖出数量，负数让卖出数量大于买入数量。取消趋势用 `--modify --trend 0`。
- `--avg` / `--trend` 只保留为基础下单 size 的兼容配置，不再驱动 top-up、密度或动态间距维护。
- 修改 `--gap` 只影响后续 P0/P2/P3/P4/P6 生成或外移的订单；既有 active 单不会被主动改价。
- `--min 20`：每张子单价值至少 20；不填时按交易所最小名义价值。
- `--total`、旧 `--max` 和旧 `--long` / `--short` / `--abs` 都不再作为推荐 grid 参数；新命令使用 `--limit MIN MAX`。
- P0 `panic`：沿用 `panic_ratio` 触发条件。先用 IOC reduce-only 市价减仓；确认成交后，以实际 `avgPx` 为锚点，在反方向 `2 * gap` 处直接提交 `grid_leg=1` GTC 单。空 grid 没有可用于计算 ratio 的 active 减仓格时，P0 不会凭空出生订单。
- P1 `terminal`：当账户 `withdrawable < 10` 时，每个账户（跨 DEX 合并计算）每轮只撤一张 `grid_leg=0` active 单。先选非 reduce-only，再选 reduce-only；同组按交易所订单时间从旧到新。交易所确认撤单成功后才从 batch 移除，后续不再维护。
- P2 `replacement`：处理确认成交的 active 格子单，以实际成交价为锚点在反方向 `1 * gap` 提交 ALO。每次成交都翻转格属性 `0 ↔ 1`；提交前按当前仓位重新判断增仓或减仓，减仓统一 reduce-only。
- P3 `debt`：所有未能挂出的 `grid_leg=1` 都是必须重试的链债务；只有交易所明确返回保证金不足时状态才记为 `margin`，超时、限流、网络或其他提交失败记为 `chain_debt`。两者均在 `withdrawable > 5` 且 raw deficit `< 0` 时重新提交。`grid_leg=0` 提交失败则直接终结。
- P4 `limit-chase`：raw deficit `< 0` 且 signed 仓位 value 仍在 `--limit` 之外时，按原方向逻辑提交一张 IOC 市价回归单。市价纯减仓（方向正确且数量不超过当前仓位）不受 `withdrawable` 限制；会加仓或穿过零仓反向开仓的动作仍要求 `withdrawable > 5`。确认成交后，以实际 `avgPx` 为锚点，在反方向 `2 * gap` 处提交 `grid_leg=1` GTC 单。每轮全局最多出生一组，也是新空 grid 唯一的出生源。
- P5 `anomaly`：只在 raw deficit `< -100` 时运行。记录中的 OID 异常消失且确认未成交时，`grid_leg=1` 当轮恢复为同方向、同价格、同属性的非 reduce-only ALO 单；`grid_leg=0` 直接从 batch 移除。
- P6 `legacy-pause`：仅用于过渡。升级时现有 active 统一记为 `grid_leg=0`，现有 paused 统一转为 `legacy_pause + grid_leg=1`。当 `withdrawable > 5` 时，每个账户（跨 DEX 合并计算）每轮只恢复一张相对盘口最近的 legacy pause；恢复完成后进入普通生命周期。
- 每轮严格按 `P0 → P1 → P2 → P3 → P4 → P5 → P6` 执行。P0/P1/P2 必须扫描；P3/P4 仅在 raw deficit `< 0` 时执行；P5 仅在 raw deficit `< -100` 时执行。P6 只受自己的过渡余额条件控制。
- P2/P3/P5/P6 的限价单使用 ALO。若价格已经穿盘、ALO 被 post-only 拒绝，或与同侧已有 active 单距离不足，会沿远离盘口方向每次外移 `1 * gap` 后继续尝试；距离检查只看真实 active 单，不把旧 paused 记录当占位。
- P0/P4 派生单直接使用 GTC。P0/P4 的 IOC 市价单本身没有 `grid_leg`，只有确认成交后派生的反向格子单带属性。市价提交前会先持久化带 `cloid` 的出生意图；写请求超时或进程中断后，下一轮按 `cloid` 查单并以实际成交价、数量补建 `grid_leg=1`，避免市价已成交但反向链债务丢失。
- v2 生命周期不再执行 top-up、每侧 active-cap、固定 16 格、dense regrid、ROE/risk-density 暂停、普通 pause/recovery 或保活单逻辑。
- Worker 继续输出 Info/Exchange/API 初始化调用的耗时统计，并把完整明细追加到 `logs/trail-api-timing.jsonl`。

### 修改

```bash
BTC grid --modify --limit -500 500
BTC grid --modify --limit -300 0
BTC grid --modify --limit 200 500
BTC grid --modify --gap 0.3%
BTC grid --modify --gap 0
BTC grid --modify --trend 0
BTC grid --modify --avg 200
BTC grid --modify --min 20
BTC grid --modify --add 10
BTC grid --modify --add -10
xyz:SPCX grid --reverse
```

- 所有 `--modify` 都只更新策略配置，不会主动撤销或重铺现有 grid 子单；新参数从以后生成的新单开始生效。
- `--modify --add N` 会把当前 signed `limit` 的上下限同时加上 `N` 美元；如果当前配置了 `avg`，也会同时加上 `N`。例如 `--limit -300 300 --avg 50` 执行 `--modify --add 10` 后变为 `--limit -290 310 --avg 60`；`--add -10` 则三项都减去 10。未设置 `avg` 时只平移 `limit`。
- `grid --reverse` 会原地翻转当前策略的 signed 仓位范围和 `avg`，不会主动撤销或重铺现有子单。例如 `--limit -25 500 --avg 50` 会变为 `--limit -500 25 --avg -50`；未设置 `avg` 时仍保持未设置。
- 模式、仓位范围或最低下单额变化不会触发旧式批量撤单；现有订单成交后的下一张 replacement 才按最新配置和仓位重新判断方向与 reduce-only。
- 从 `avg` 模式切回无方向的普通网格可使用 `--modify --trend 0`。
- `--modify` 只改变命令中明确提供的参数；例如只传 `--trend` 时会沿用原来的 gap，只传 `--gap` 时也会沿用原来的 trend。`--modify --gap 0` 会按当前价格和费率重算默认 gap。
- 修改 `--limit` 后，P4 从下一轮起按新范围判断是否需要出生一组 limit-chase。

### 查询、恢复、取消

```bash
BTC grid --recover --limit -300 300 --gap 0.5%
BTC grid --query
BTC --cancel grid
```

- `BTC grid --recover --limit -300 300 --gap 0.5%` 会把当前该币普通 limit open orders 接管为 `grid_leg=0`，用于服务器断点或 JSON 丢失后的人工恢复。
- `BTC grid --query` 会展示该币 grid 的 limit/min/gap/仓位、买卖两边 active 数量、每张子单的 oid/价格/状态/live 情况和最近成交。
- `BTC --cancel grid` 会取消服务器维护的网格和所有活跃子单。

### Worker 行为

- 新建 grid 只保存配置和空 `levels`，不向交易所提交初始限价单。仓位在 limit 内时，空 grid 会保持为空。
- active 格子成交后由 P2 立即接续；优先使用成交历史中的实际成交价。若交易所已确认该限价单曾经 `resting`，但成交历史因接口条数上限缺失，则其完全成交价就是原挂单价，允许用保存的价格和数量接续；未确认 resting 的穿盘 GTC/IOC 仍必须等待实际 `avgPx`。
- 每次提交前都根据最新仓位和订单方向重新判断增仓/减仓。减仓单使用 reduce-only，增仓单不使用 reduce-only；P5 异常恢复按规则强制使用非 reduce-only。
- `grid_leg=1` 的接续单若未能挂出，会按真实失败原因保留为 `margin` 或 `chain_debt` 等待 P3；`grid_leg=0` 的接续单遇到失败直接终结，不形成长期暂停队列。
- P1 撤单和 P6 恢复都以账户为配额单位，避免同一账户下多个币种在一轮内同时集中动作。
- grid/trail 创建、修改和取消与 Worker 共用 `server_batch.lock`；命令执行期间 Worker 会跳过该轮，避免旧任务快照覆盖刚完成的修改。
- 旧任务第一次进入 v2 Worker 时自动迁移：active 记为 leg 0，paused 记为 legacy pause/leg 1。P6 清空过渡队列后，旧 pause 状态不再产生。

### 接续和边界

- worker 每次至少回看最近 24 小时成交记录；如果 `last_fill_check_ms` 更早，会从更早的断点继续查。
- worker 只维护 `server_batch.json` 里记录的 oid；手动下的新单不会被自动接管。
- OID 从 open orders 消失时，worker 先结合最近成交和订单状态判定：确认成交走 P2，确认取消或异常消失走 P5，尚不能确认则保留记录等待下一轮，避免把延迟成交误判为撤单。
- P5 只恢复未闭合的 leg 1，且恢复为非 reduce-only；已经完成往复的 leg 0 直接清除，不再重建。
- 服务器断电或 worker 重启后，只要 `server_batch.json` 还在，active、margin、chain debt、出生意图和 legacy pause 都会从保存状态继续处理。

## 服务器 Trail 单

`--trail` 是由本工具维护的服务器跟踪止损，不是 Hyperliquid 原生 trailing stop。下单时会先提交一张 stop market 触发单，再把任务写入 `server_batch.json`，后续由 `trail_worker.py` 定时检查价格并修改 stop 触发价。

```bash
# 按当前 mid 回撤 2% 做跟踪卖出
BTC sell 50 --trail 2%

# 允许反手开空；如果只想减仓，显式加 reduce-only
BTC sell 50 --trail 2% --reduce-only

# 查看账户时会展示 Server Batch
query
BTC

# 取消当前 BTC 的 trail 任务
BTC --cancel trail
```

运行逻辑：

- `--trail` 不和 `--price` 混用，锚定价始终用下单时的当前 mid。
- `BTC sell 50 --trail 2%` 会先按 `mid * (1 - 2%)` 挂 stop market；价格继续上涨时，worker 会抬高 stop。
- `BTC buy 50 --trail 2%` 会先按 `mid * (1 + 2%)` 挂 stop market；价格继续下跌时，worker 会压低 stop。
- `amount` 在任务创建后固定，后续只改 stop 触发价，不重新计算金额。
- `reduce_only` 默认是 `False`，允许开仓、减仓或反手；要保护已有仓位时请加 `--reduce-only`。
- 如果原 stop 单已经成交或不再 open，worker 会把任务标记为 `done`。
- `done` 记录最多保留 7 天或 500 条，避免历史记录无限膨胀。
- 已确认 `cancelled` 的整条 grid 和 grid level 不再保留在 `server_batch.json`；`pending_cancel`、`filled` 和 paused 状态仍按原逻辑跟踪或裁剪。

服务器定时和负载：

- `systemd/simple-hyper-trail-worker.timer` 默认每分钟触发一次 `trail_worker.py`。
- `trail_worker.py` 是 one-shot：每次处理当前 `active` trail/grid 任务，处理完即退出。
- 如果上一轮运行超过 1 分钟，文件锁会让下一轮直接跳过，避免并发改同一批订单。
- 单轮运行超过 3 分钟时，systemd 会终止本轮；之后的定时轮次会继续正常运行。
- 没有 `active` 任务时，worker 只打印 `trail_worker: no active trail/grid orders`，不会查询价格或订单。
- 当前同一个 worker 同时维护 trail 和 grid。实际耗时主要取决于 Hyperliquid API 网络等待；近期 4 个 grid 同时维护时，每轮墙钟约 10-30 秒，CPU 通常约 1 秒。

查询和取消：

- `query` / `BTC` 会读取 `server_batch.json`，在 `Server Batch` 表里展示 trail 状态。
- 属于 active/error trail 的 oid 会从 `Open Orders` 表中过滤掉，避免重复显示。
- `BTC --cancel trail` 会匹配 `active` 和 `error` 状态的 trail 任务，先撤链上订单，再把 batch 标为 `cancelled`。
- 普通 `BTC --cancel` 如果撤到了 batch 里的 oid，也会同步把对应任务标为 `cancelled`。

状态含义：

- `active`：worker 会继续跟踪。
- `done`：链上订单已不再 open，通常是已成交或被外部撤掉。
- `cancelled`：由本工具撤单并停止跟踪。
- `error`：非临时错误，需要人工查看 `error` 字段后处理。

临时限流和错误：

- Hyperliquid / CloudFront 返回 `429/502/503/504` 时，worker 不会把任务停成 `error`。
- 这类错误会保留任务 `active`，写入 `last_error`，并在下一轮继续重试。
- 限流日志追加到 `logs/trail-rate-limit.jsonl`，每行一个 JSON，后续可按 `status_code`、`oid`、`coin` 统计频次。
- 非临时错误才会把任务标成 `error`；旧版 grid 的历史 post-only 拒绝仍属于可恢复情况，不会停掉任务。

## 触发单和止盈止损

`--stop` / `--take` 是入场触发价：

- `--stop` 偏突破入场。
- `--take` 偏触价入场。
- 不写偏移时是触发市价单。
- 写成 `70000+50`、`70000-50`、`70000+0.2%` 时会变成触发限价单。
- 百分比按触发价计算，所以 `70000+2% = 71400`；如果想要 `70140`，写 `70000+0.2%`。

示例：

```bash
# 突破后开仓
BTC sell 25 --stop 70000
BTC buy 25 --stop 80000

# 突破后开仓，并指定触发限价
BTC sell 25 --stop 70000+50
BTC buy 25 --stop 80000+100

# 触价后开仓
BTC buy 25 --take 70000
BTC sell 25 --take 80000

# 触价后开仓，并指定触发限价
BTC buy 25 --take 70000+50
BTC sell 25 --take 80000+100
```

`--tp` / `--sl` 支持绝对价、相对百分比和限价偏移：

```bash
# 保护已有多仓：到价后卖出平仓
BTC sell 25 --tp 72000 --reduce-only
BTC sell 25 --sl 65000 --reduce-only

# 按持仓均价计算相对止盈止损
BTC sell --tp 2%+0.1% --sl 2%-0.1% --reduce-only

# 开仓同时挂止盈止损
BTC buy 100 --price 68000 --tp 72000 --sl 65000

# 触发入场，并只处理 60% 的止盈数量
BTC buy 30 --stop 80000-10 --tp 0.6%+0d0.6
```

百分比不写正负号时，程序按方向自动判断：

- `buy/long`：`--tp 2%` 表示上涨 2% 止盈，`--sl 2%` 表示下跌 2% 止损。
- `sell/short`：`--tp 2%` 表示下跌 2% 止盈，`--sl 2%` 表示上涨 2% 止损。
- 仍然可以显式写 `+2%` 或 `-2%`，显式符号优先。

数量比例后缀：

- 在 `--tp` / `--sl` 后面加 `d0.6` 或 `d60%`，表示按比例处理这笔单子。
- 当前允许范围是 `0 < ratio <= 2`，可以用于部分止盈，也可以用于不对称卖出。

组合规则：

- `--tp` / `--sl` 不加 `--reduce-only` 时，会用 `normalTpsl` 一次提交开仓单和子止盈 / 止损单。
- `--tp` / `--sl` 加 `--reduce-only` 时，会按 `positionTpsl` 提交保护已有仓位的触发单。
- `--stop` / `--take` 可以和 `--tp` / `--sl` 组合成 entry-trigger bracket。

## 分批和梯子单

`--scale` 会把总金额平均拆成多张限价单：

```bash
BTC buy 100 --scale 5 --from 67000 --to 63000
BTC sell 200 --scale 4 --from 72000 --to 76000 --reduce-only
```

梯子单把每一档当成独立订单，使用显式参数：

```bash
# 从 67000 开始，每档差 1000，一共 5 档
BTC buy --for 5 -1000 --price 67000

# 从 80000 一路挂到 85000，每档差 1000
BTC sell --while 85000 +1000 --price 80000

# 普通梯子 + 每档自己的 TP/SL
BTC buy --for 5 -1000 --price 67000 --tp 5%+0 --sl 2%-10

# 触发梯子，只做减仓
BTC sell 10 --while 80000 +1000 --stop 77000 --reduce-only

# 更小步长示例
HYPE buy 13 --for 10 -0.2 --tp 0.05%+0.01%d0.9
HYPE buy 12 --while 65 -0.05 --tp 0.07%+0.01d0.9

# 总金额 120，自动均分到 10 档
HYPE buy --total 120 --for 10 -0.05 --tp 0.07%+0.01d0.9

# 到 68 为止一共 3 档，步长自动计算
HYPE buy --total 50 --while 68 --for 3 --tp 0.07%+0.02d0.9

# 明确起点、终点、步长
HYPE buy 12 --range 66 65 -0.05 --tp 0.07%+0.01d0.9

# 明确起点、终点、档数，步长自动计算
HYPE buy --total 50 --range 68.5 68 --for 3 --tp 0.07%+0.02d0.9

# 只解释解析结果，不提交
HYPE buy --total 120 --range 66 65 -0.05 --tp 0.07%+0.01d0.9 --explain
```

注意：

- `--scale` 每张子单金额必须至少 `10` 美元。
- `--for COUNT STEP` 表示从当前基准价格开始，按 `STEP` 间隔下 `COUNT` 档。
- `--while END STEP` 表示从当前基准价格开始，按 `STEP` 间隔下到 `END` 为止。
- `--while END --for COUNT` 表示从当前基准价格开始，到 `END` 为止一共 `COUNT` 档，程序自动计算步长。
- `--range START END STEP` 表示从 `START` 开始，按 `STEP` 间隔下到 `END` 为止，等价于 `--price START --while END STEP`。
- `--range START END --for COUNT` 表示从 `START` 到 `END` 一共 `COUNT` 档，程序自动计算步长。
- `--total TOTAL` 在梯子单或对称单里表示总金额，会按实际档数均分；不写 `--total` 时，位置参数 `amount` 是每一档金额。
- `STEP` 必须带方向符号，例如 `-0.2`、`+1000`、`-0.5%`。
- 普通梯子可以再配 `--tp` / `--sl`，每一档都会带自己的 bracket。
- 触发梯子可以再配 `--stop` / `--take`，但不能再把 `--tp` / `--sl` 放进同一条命令。

## 标的

官方可交易标的以 Hyperliquid `meta` API 返回的名字为准，不以 App URL 为准。比如 App 路由能打开 `xyz:SAMSUNG`，但 API 真实标的是 `xyz:SMSN`。
本工具不再维护本地标的别名，也不会把 `BTCUSD`、`BTC-PERP`、`SAMSUNG` 这类输入自动改写成其他 API 标的；下单时请直接输入真实标的名。

查询官方标的：

```bash
markets BTC
markets GOLD
markets --dex xyz --limit 10
./hl_markets.py --csv > perp_markets.csv
```

## macOS 和 Windows

macOS 专用终端：

```text
order-terminal.command
```

普通终端：

```bash
./order BTC buy --dry-run
```

Windows 首次安装：

```text
setup-windows.cmd
```

Windows 日常使用：

```text
order-terminal-windows.cmd
```

或者在 CMD / PowerShell 里运行：

```bat
order.cmd BTC buy --dry-run
query.cmd
markets.cmd QQQ
```

## 手机网页控制台

`simple_hyper_server.py` 是给 iPhone/Safari 用的极简网页控制台。它不会重写交易逻辑，只是在服务器上调用当前目录的 `hl_order.py`。

本地或服务器启动：

```bash
cp simple-hyper.env.example simple-hyper.env
set -a
. ./simple-hyper.env
set +a
python3 simple_hyper_server.py --host 0.0.0.0 --port 8787
```

网页控制台等待命令完成的默认超时是 `300` 秒，可通过 `SIMPLE_HYPER_COMMAND_TIMEOUT` 调整。循环单档数较多时，建议保持 `300` 秒或更高。HTTP 连接读取默认超时为 `15` 秒；同时最多接受 `32` 个连接、运行 `2` 个下单子进程，可分别通过 `SIMPLE_HYPER_CONNECTION_READ_TIMEOUT`、`SIMPLE_HYPER_MAX_CONNECTIONS` 和 `SIMPLE_HYPER_MAX_CONCURRENT_COMMANDS` 调整。命令容量已满时 API 快速返回 HTTP `503`。

访问：

```text
http://服务器地址:8787
```

如果设置了 `SIMPLE_HYPER_TLS_CERT` 和 `SIMPLE_HYPER_TLS_KEY`，则使用：

```text
https://服务器地址:8787
```

安全设计：

- 服务器本地 `.env` 或环境变量保存 `account_address` 和 `secret_key`。
- 网页不显示、不接收、不保存私钥；浏览器请求只发送命令和页面参数。
- 后端每次执行时从服务器本地读取凭证，再只放进子进程环境变量。
- 命令历史保存在服务器本地 `command_history.json`，不同设备访问同一服务器会显示同一份历史。
- 后端用 `shlex.split()` 解析输入框命令，不走 shell。
- 命令不含 `--dry-run` 且不是 `query` 时，前端会弹出真实提交确认。
- 服务日志不记录请求 body，`hl_order.py` 日志也不会写入密钥。

systemd 常驻可参考：

- `systemd/simple-hyper-sync.service`
- `systemd/simple-hyper-sync.timer`
- `systemd/simple-hyper.service`
- `scripts/simple-hyper-sync.sh`

服务器权限建议：`/opt/simple-hyper` 及其父目录必须由 `root` 所有且不可由 `simplehyper` 写；仅将 `/var/lib/simple-hyper` 运行时状态目录交给 `simplehyper` 写。首次部署先创建服务用户，并安装环境文件、root 执行副本和 units：

```bash
sudo useradd --system --home /var/lib/simple-hyper --shell /usr/sbin/nologin simplehyper
sudo install -o root -g root -m 0755 scripts/simple-hyper-sync.sh /usr/local/sbin/simple-hyper-sync.sh
sudo install -o root -g simplehyper -m 0640 simple-hyper.env /opt/simple-hyper/simple-hyper.env
sudo install -o root -g root -m 0644 systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simple-hyper.service simple-hyper-trail-worker.timer simple-hyper-sync.timer
```

同步脚本的 root 副本属于部署边界；仓库中的同步脚本有更新时，应重新执行对应的 `install` 命令。同步会迁移旧的 `server_batch.json`、`server_batch.lock`、`command_history.json` 和 `logs/`，并在服务成功重启后才记录新 ETag。`simple-hyper.env` 和 `.env` 会在 rsync 更新时保留。

常用命令：

```bash
sudo systemctl restart simple-hyper.service
systemctl status simple-hyper-sync.timer
journalctl -u simple-hyper-sync.service -n 80 --no-pager
```

## 输出和日志

前台输出只显示账户指标和订单核心字段。数字统一使用千分位并保留 2 位小数。

```text
+- Account ----------------+
| withdrawable(USDC): 0   |
| 统一账户比率: 0.19%        |
| 统一账户杠杆: 0.09x        |
| legacy_pause: 0         |
+----------------------------+
+- Order ------------------+
| coin: xyz:XYZ100           |
| side: B                    |
| midPx: 29,812.00 (725.55)  |
| limitPx: 29,889.00         |
| amount: 10.46              |
+----------------------------+
```

字段说明：

- `side=B`：买入 / 看多。
- `side=A`：卖出 / 看空。
- `midPx`：当前中间价；如果别名有 `rate`，括号里显示换算价。
- `limitPx`：交易所返回的挂单价格。
- `amount`：按挂单价格和数量计算出的实际名义金额。

完整日志保存在：

```text
logs/
```

服务器 trail 限流日志：

```text
logs/trail-rate-limit.jsonl
```

排错时可以加：

```bash
BTC buy --dry-run --verbose
```

如果出现 `Unknown perp coin`，优先用 `markets` 查真实 API 标的，然后在下单命令里直接使用真实标的。

## 项目结构

```text
hl_order.py              # 下单 / 查询主入口
hl_markets.py            # 标的查询入口
trail_worker.py          # server_batch.json 的 trail 任务维护器
simple_hyper/            # 可复用 Python 业务代码
simple_hyper_server.py   # 手机网页控制台
server_batch.json        # 本地/服务器运行时 batch 状态，不提交 Git
command_history.json     # 网页命令历史，不提交 Git
scripts/                 # 运维脚本
systemd/                 # systemd 单元
requirements.txt         # Python 依赖
```

设计约定：

- `hl_order.py` / `hl_markets.py` 保留为命令入口，现有 `order`、`query`、`.cmd` 和网页服务调用方式不变。
- `simple_hyper/` 放可导入业务代码：运行环境、控制台格式化、K 线渲染、订单解析、价格和数量计算。
- `scripts/` 只放 shell/systemd 这类运维脚本。
- 下单路径复用 `Exchange` 内部已经初始化好的 `Info`，减少重复初始化和重复拉取元数据；`query` 路径不创建交易客户端。

## 环境和依赖

`.venv/` 是当前目录专用 Python 虚拟环境，由首次安装命令创建。公开仓库不提交 `.venv/` 和本地 SDK 源码目录，依赖统一由 `requirements.txt` 安装。

当前 SDK 信息：

- SDK：`0.23.0`
- commit：`7ee976d123b1e04295e4a1e37a424ca6a13bef88`
- 官方仓库：https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- 官方 API 文档：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api

核心依赖：

```text
hyperliquid-python-sdk
eth_account
requests
websocket-client
msgpack
```

如果依赖损坏，可以删掉 `.venv/` 后重新执行首次安装命令。

## 技术备注

价格和数量规则：

- 价格最多 `5` 个有效数字。
- 永续价格小数位最多 `6 - szDecimals`。
- 现货价格小数位最多 `8 - szDecimals`。
- 数量精度由该资产的 `szDecimals` 控制。
- Hyperliquid 同一合约是净仓位逻辑，不会同时存在一条多仓和一条空仓。

`reduce_only` 规则：

```text
reduce_only=False：允许开仓、加仓、减仓、反手
reduce_only=True：只允许减仓 / 平仓，不允许反手
```

统一账户指标口径：

- `withdrawable(USDC)`：`balances[USDC].total - balances[USDC].hold`，与 Worker 的可提余额保护使用同一口径。
- 统一账户比率：将各 DEX 的 maintenance margin 按 collateral token 聚合，再除以对应 spot 抵押品余额，取风险最高的一组。
- 统一账户杠杆：当前总名义仓位 / 活跃抵押品余额。

SDK 用法：

```python
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

info = Info(constants.MAINNET_API_URL, skip_ws=True)
print(info.all_mids()["BTC"])

wallet = eth_account.Account.from_key("0x你的私钥")
exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address="0x主账户地址")
```
