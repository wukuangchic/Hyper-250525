# Hyperliquid 本地下单工具

一个基于官方 `hyperliquid-python-sdk` 的本地下单工具，支持本地终端、Windows CMD 和手机网页控制台。

日常推荐：

- macOS：双击 `order-terminal.command`，打开后可以直接输入 `BTC buy`、`query`。
- 普通终端：先执行 `. ./aliases`，再使用 `order BTC buy`。
- Windows：双击 `order-terminal-windows.cmd`。

> 真实下单默认会提交到 Hyperliquid。新命令建议先加 `--dry-run` 预演。

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

# 持仓表里的 realPnl / result 会用历史成交、手续费、资金费和当前持仓浮盈亏重算真实盈亏

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

- `coin`：标的，例如 `BTC`、`ETH`、`GOLD`、`xyz:SMSN`。
- `side`：方向，支持 `buy/sell`、`long/short`、`看多/看空`，也支持 `both/sym/对称`。
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
BTC sym --total 200 --offset 2% --explain

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

`grid` 是本工具维护的真实挂单网格：下到交易所的是普通 GTC limit 单，不是 Hyperliquid 原生网格。服务器 worker 每分钟检查一次，目标是让买卖两边各保持最多 5 张活跃子单；成交后按原子单提交限价和 `gap` 补下一张反向 limit 单。

### 创建

```bash
BTC grid --abs 300 --gap 0.5% --trend 10%
BTC grid --long 300 --gap 0.5% --min 20
BTC grid --long 200 300 --gap 0.5%
BTC grid --short 300
```

仓位限制三选一：

- `--long 200`：只允许开多头仓位，持仓价值范围为 0–200；卖单只用于已有多仓的 reduce-only 减仓。
- `--long 100 200`：只允许开多头仓位，并把持仓价值维持在 100–200；低于 100 时不再减仓，高于 200 时不再加仓。
- `--short 200`：只允许开空头仓位，持仓绝对价值范围为 0–200；买单只用于已有空仓的 reduce-only 减仓。
- `--short 100 200`：只允许开空头仓位，并把持仓绝对价值维持在 100–200。
- `--abs 200`：多空都可以开，最大绝对持仓价值 200。
- `--abs` 只接受一个最大值，不设置最低绝对仓位。

参数：

- `--gap`：每个买卖格子的间距。初始买价从 `mid * (1 - gap)` 到 `mid * (1 - 5 * gap)`，卖价从 `mid * (1 + gap)` 到 `mid * (1 + 5 * gap)`。
- 不写 `--gap` 时，默认使用 `最小价格变动百分比 + 折扣后 takerFee + 折扣后 makerFee`。
- `--trend`：数量倾向，默认 `0`；正数让买入数量大于卖出数量，负数让卖出数量大于买入数量。取消趋势用 `--modify --trend 0`。
- `--min 20`：每张子单价值至少 20；不填时按交易所最小名义价值。
- `--total` 和旧 `--max` 不再用于 grid；如果写了会直接报错。
- grid 子单允许在限价以内立即成交并获得价格改善，可能按 taker 计费；网格档位仍按提交限价推进，不因实际成交价更优而漂移。

### 修改

```bash
BTC grid --modify --abs 500
BTC grid --modify --short 300
BTC grid --modify --long 200 500
BTC grid --modify --gap 0.3%
BTC grid --modify --trend 0
BTC grid --modify --min 20
```

- 同模式修改额度，例如 `--modify --abs 500`，只更新持仓限制配置，不强制撤单重铺。
- 模式变化，例如 `--modify --long 200` 改成 `--modify --short 200`，会撤掉当前活跃 grid 子单，包括旧 reduce-only 单，再按新模式重铺。
- 修改 `--gap`、`--trend` 或 `--min` 会撤掉当前活跃 grid 子单，再按当前行情和新配置重铺。
- `--modify` 只改变命令中明确提供的参数；例如只传 `--trend` 时会沿用原来的 gap，只传 `--gap` 时也会沿用原来的 trend。
- 修改 `--long` / `--short` 的下限会按新仓位范围重铺；账户安全余量率低于 70% 时，账户保护仍优先，Worker 不会为了达到下限而新增风险。

### 查询、恢复、取消

```bash
BTC grid --recover --abs 300 --gap 0.5%
BTC grid --query
BTC --cancel grid
```

- `BTC grid --recover --abs 300 --gap 0.5%` 会从当前该币普通 limit open orders 里按近侧最多每边 5 张接管到 `server_batch.json`，用于服务器断点或 JSON 丢失后的人工恢复。
- `BTC grid --query` 会展示该币 grid 的 limit/min/gap/仓位、买卖两边 active 数量、每张子单的 oid/价格/状态/live 情况和最近成交。
- `BTC --cancel grid` 会取消服务器维护的网格和所有活跃子单。

### Worker 行为

- 到达持仓上限后，worker 会撤销继续加仓方向的 grid 单，只保留/恢复减仓方向。
- 仓位降到能容纳下一张加仓单后，worker 再把加仓方向补回到最多 5 张。
- 补缺失子单时优先参考盘口 best bid/ask，而不是只参考 mid；盘口读取失败时才退回 mid。
- 到达持仓上限后，如果减仓方向最靠近市场的单已经离盘口超过约 5 个 gap，worker 会补一张新的近侧 reduce-only 平仓单，再撤远侧，保持每边最多 5 张。
- 全仓或逐仓的加仓方向若被交易所以保证金不足拒绝，worker 会对该方向冷却 10 分钟，本轮不再继续试单；减仓方向照常维护。仓位缩小会提前解除冷却，否则到期后探测一次。
- reduce-only 子单的活动数量总和不会超过当前可减仓数量；交易所因可减仓数量不足自动取消的 `reduceOnlyCanceled` 不会被当作手动撤单反复补回。
- 每轮只查询一次账户 USDC 的“维护保证金后余量 / 总余额”。比例低于 `70%` 时不修改已有挂单，但所有新 grid 子单必须减少当前净仓位并强制使用 reduce-only；会加仓或反手的新单暂缓，比例恢复到 `70%` 后自动继续。
- 旧版保存的 Alo 子单在 Worker 再次提交时会自动升级为 GTC；历史 post-only 拒绝仍按可恢复状态兼容处理。

### 接续和边界

- worker 每次至少回看最近 24 小时成交记录；如果 `last_fill_check_ms` 更早，会从更早的断点继续查。
- worker 只维护 `server_batch.json` 里记录的 oid；手动下的新单不会被自动接管。
- 如果 worker 发现 grid oid 消失但最近成交记录里找不到对应 fill，会按该记录原来的 side、price、size 重新挂回，并在任务 note 里记录 `recovered_missing`。
- 如果服务器断电或 worker 重启，只要 `server_batch.json` 还在，下一轮会继续按已有 oid 和最近成交接续维护。

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

## 标的和别名

官方可交易标的以 Hyperliquid `meta` API 返回的名字为准，不以 App URL 为准。比如 App 路由能打开 `xyz:SAMSUNG`，但 API 真实标的是 `xyz:SMSN`。

本地别名维护在 `coin_aliases.csv`：

```csv
alias,target,note,rate
GOLD,xyz:GOLD,Hyperliquid app route uses builder DEX xyz,
SAMSUNG,xyz:SMSN,Samsung Electronics on xyz,
QQQ,xyz:XYZ100,Nasdaq100,41.09
```

字段说明：

- `alias`：日常输入的名字。
- `target`：Hyperliquid API / SDK 实际下单用的标的。
- `note`：备注。
- `rate`：可选换算倍率。前台价格会显示为 `原始报价 (原始报价/rate)`。

这些命令都可以使用：

```bash
GOLD buy
SAMSUNG buy
QQQ buy
xyz:GOLD buy
xyz:SMSN buy
```

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
. ./aliases
order BTC buy --dry-run
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

网页控制台等待命令完成的默认超时是 `300` 秒，可通过 `SIMPLE_HYPER_COMMAND_TIMEOUT` 调整。循环单档数较多时，建议保持 `300` 秒或更高。

访问：

```text
http://服务器地址:8787
```

如果设置了 `SIMPLE_HYPER_TLS_CERT` 和 `SIMPLE_HYPER_TLS_KEY`，则使用：

```text
https://服务器地址:8787
```

安全设计：

- 服务器不需要保存交易 `.env`。
- 网页验证通过后会隐藏地址和密钥，凭证只留在当前页面内存里。
- 每次执行请求都会带 `account_address` 和 `secret_key`，后端只放进子进程环境变量。
- 后端用 `shlex.split()` 解析输入框命令，不走 shell。
- 命令不含 `--dry-run` 且不是 `query` 时，前端会弹出真实提交确认。
- 服务日志不记录请求 body，`hl_order.py` 日志也不会写入密钥。

systemd 常驻可参考：

- `systemd/simple-hyper-sync.service`
- `systemd/simple-hyper-sync.timer`
- `scripts/simple-hyper-sync.sh`

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
| 账户安全余量率: 47.64%     |
| Grid保护(<70%): 开启       |
| 统一账户比率: 0.19%        |
| 统一账户杠杆: 0.09x        |
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

如果出现 `Unknown perp coin`，优先用 `markets` 查真实 API 标的，再把常用缩写写入 `coin_aliases.csv`。

## 项目结构

```text
hl_order.py              # 下单 / 查询主入口
hl_markets.py            # 标的查询入口
trail_worker.py          # server_batch.json 的 trail 任务维护器
simple_hyper/            # 可复用 Python 业务代码
simple_hyper_server.py   # 手机网页控制台
coin_aliases.csv         # 本地标的别名
server_batch.json        # 本地/服务器运行时 batch 状态，不提交 Git
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

- 账户安全余量率：`tokenToAvailableAfterMaintenance[USDC] / balances[USDC].total`；低于 `70%` 时，新 grid 子单只允许 reduce-only 减仓。
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
