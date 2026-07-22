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

`grid` 是本工具维护的真实挂单网格：下到交易所的是普通 ALO limit 单，不是 Hyperliquid 原生网格。服务器 worker 每分钟检查一次，目标是让买卖两边各至少补到 16 张活跃子单；成交后按原子单提交限价和 `gap` 补下一张反向 limit 单。成交反向单或恢复单可能让一侧超过 16 张，worker 不会仅因超过 16 张而主动撤单。

旧任务中保存的默认每侧 5 张或 10 张会由 worker 自动迁移为每侧 16 张；每轮每侧最多提交 1 张，因此会分多轮逐步补齐。

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

- `--gap`：每个买卖格子的间距。初始买价从 `mid * (1 - gap)` 到 `mid * (1 - 5 * gap)`，卖价从 `mid * (1 + gap)` 到 `mid * (1 + 5 * gap)`。
- 不写 `--gap`，或写 `--gap 0` / `--gap 0%` 时，默认使用 `最小价格变动百分比 + 折扣后 takerFee + 折扣后 makerFee`。
- `--trend`：数量倾向，默认 `0`；正数让买入数量大于卖出数量，负数让卖出数量大于买入数量。取消趋势用 `--modify --trend 0`。
- `--avg 200`：把 200 设为目标持仓价值，并与 `--trend` 互斥。`avg` 只调整补档间距，不调整单量；回归侧保持基础 `gap`，偏离目标的一侧按偏离度指数放大补档 `gap`。偏离 25% 时约 `1.46` 倍，50% 时约 `1.74` 倍，接近上下限时倍率趋向无限大。
- `--avg` 使用同样的 signed 仓位价值；例如 `--limit -300 300 --avg -100` 表示目标为空仓 100。
- `avg` 模式下买卖基础单量保持一致；订单低于交易所最小名义价值时，仍沿用最小下单额保护。
- 成交反向单始终使用基础 `gap` 和基础单量。paused 恢复和创建时的初始网格使用基础参数，且不会仅因仓位变化撤掉旧挂单。
- 修改 `--gap` 只影响后续成交反向单、常规补档和恢复提交；既有 active 单不会被改价，密集去重也优先按旧单保存的 `plan.grid_gap` 判断，避免新 gap 反向清掉旧网格。
- `--min 20`：每张子单价值至少 20；不填时按交易所最小名义价值。
- `--total`、旧 `--max` 和旧 `--long` / `--short` / `--abs` 都不再作为推荐 grid 参数；新命令使用 `--limit MIN MAX`。
- grid 子单使用 ALO，目标是只挂 maker 单；网格档位仍按提交限价推进，不因实际成交价更优而漂移。
- 最新成交反向单的目标价若只被同侧可恢复 `paused_*` 档位挡住，会保留目标价并把这些 paused 标记逐级向外顺推（buy 向低价、sell 向高价），同步更新完整价格计划；若同时有 active、pending、pending_cancel 等真实占位，则不移动 paused。成交反向单如果 ALO 因会立即成交被拒，会先确认同侧前后 `0.95 * gap` 内没有真实占位；若不太近，会先向远离盘口方向移动一个 `gap`，若太近才优先插入外移方向上间距大于 `1.95 * gap` 的相邻两格中间，最多 20 次。其他 grid 子单 ALO 被拒时只跳过本轮，不换价重试。
- worker 补齐每侧子单时，交易所一旦接受提交，本轮就视为已补一个名额；即使订单随后很快成交，也不会在同一轮为了凑够 16 张活跃单连续追单。
- Worker 的主动撤单统一保留每侧至少 `1` 张 active 单；当某侧需要从 paused / recovery 状态恢复时，buy 按高价优先、sell 按低价优先，让离当前盘口最近的候选先恢复。唯一安全例外是 panic 成对动作失败后清理没有减仓成交支撑的裸反转单。
- 每轮每侧最多向交易所提交 1 张普通新 grid 单；成交生成当轮的即时 replacement 可绕过提交前的方向额度检查，但成功后会占用该侧本轮额度，因此同侧常规补档和历史档位恢复留到后续轮次。
- 遇到地址 action limit 紧张时，worker 会把维护动作分级执行：
  - P0：必要安全动作，不占共享 P1 预算。当前包括 `panic_ratio` 触发的 IOC reduce-only 减仓、`paused_risk_density` 必要撤单、`paused_roe` 必要撤单；panic 后的反向普通挂单也在这段流程内，但仍会经过自身的 ALO、仓位和保证金判断。P0 不会因 headroom 不足而拦截，但每个交易所 action 都会在提交前预扣 headroom，并立即压缩后续 P1/P2 可用量。
  - P1：共享预算动作。当前包括历史 replacement 恢复、`paused_limit` 撤单、`paused_active_cap` 撤单、`refresh_reduce_only` 撤单、普通补档和 paused 恢复；同一轮这些 P1 动作共用 `action_limit_p1_budget`。成交生成当轮的即时 replacement 虽按最高 P1 优先级扫描，但绕过本地 action-limit 预拦截且不消耗 P1 预算；交易所真实拒单仍照常处理。
  - P2：非关键整理动作。当前包括 dense regrid 和 replacement rebalance；只有执行完 P0/P1 后预估 action headroom 仍大于 `100` 才会运行。
  - P3 `limit-chase`：worker 启动时记录所有 signed 仓位价值在 `--limit MIN MAX` 之外的 grid；P1 全阶段扫描完成后，仍按既定顺序等待 P2 扫描结束再进入 P3，但 P3 不受 P2 的 `action headroom > 100` 或 action-limit 错误门槛限制，即使 P2 因额度不足跳过也可执行。P3 从候选中随机选择一个币种，重新查询最新仓位与账户 USDC withdrawable。只有仓位仍在范围外且 `withdrawable > 10`（严格大于，等于 `10` 不执行）时才追回一档：低于下限按 `base_buy_size` 提交普通 IOC 市价买单，高于上限按 `base_sell_size` 提交普通 IOC 市价卖单，两者都不使用 reduce-only；若 base size 按 P3 最新 mid 计算不足 `max(交易所最低额, min_order_value) * 1.10`，会按数量精度向上扩大到该缓冲金额，避免边缘市价单被交易所按最小名义价值拒绝。市价成交后，以 IOC 回执中的实际成交均价 `avgPx` 为锚点，在反方向 `2 * gap` 处提交实际成交 size 的 GTC replacement；只有回执缺少有效 `avgPx` 时才回退到 P3 最新 mid。该单带 `limit_chase_replacement` 和 `replace_never_cancel`，与 panic replacement 一样永久保护，但它成交后生成的下一张普通 replacement 不继承永久保护。每轮 worker 全局最多执行一个币种，P3 的仓位和 withdrawable 不复用 P0-P2 的启动缓存。
- `panic` IOC、`limit-chase` IOC、panic replacement 和 limit-chase replacement 若收到交易所 `Too many cumulative requests sent`，会留在当前紧急流程中每 10 秒重试一次，直到交易所出现可提交窗口；其他错误仍立即沿用原失败处理，不进入该等待循环。
- P1 预算每轮都会预先计算，不等到确认超限后才计算。未超限时本轮共享 P1 预算为 `max(1, headroom - 1)`，即至少放行 1 次，headroom 足够时预留 1 个 action headroom；已超限时若 `deficit < 3` 仍给 1 次，若 `deficit >= 3` 则按 `1 / ln(deficit)` 的概率给 1 次，否则本轮 P1 为 0。P0、P1、P2 的挂单、撤单、必要杠杆更新和重试都会在调用交易所前按实际 action 数预扣 headroom；P0 或前序 P1 消耗后，剩余 P1 预算会实时压缩为不超过 `max(0, remaining_headroom - 1)`，失败或被拒但已经发往交易所的 action 也保守计入。这样 deficit 刚转负时不会继续按轮初旧预算集中挂单。
- Worker 每轮会先把币种顺序打乱，再按全局动作优先级分桶扫描所有 grid：`P0` > 最新成交 `replacement_pending` > 旧 `replacement_order` 恢复 > P1 撤单 > topup > 普通 paused 恢复 > `P2` > 全局单币种 `P3 limit-chase`。旧 `paused_replacement` 在 P1 批内按每个方向距当前盘口由近到远扫描：buy 侧价格从高到低、sell 侧价格从低到高；会穿盘口或仍受控制的档位跳过后继续检查下一档。每个桶都会跨所有币种扫完后才进入下一桶，因此单个币种的 topup 不会抢在其他币种的 replacement 前面消耗共享 P1 预算；最终 note 记录整轮累计动作数。
- Worker 会统计真实 Info cache-miss、Exchange 提交/撤单以及 `build_clients` 初始化的调用次数和耗时，并按 `phase / 币种 / API方法` 聚合 `count`、`total_ms`、`max_ms`、`errors`。每轮结束时 journal 输出 `trail_worker: api_stats ... top=...` 摘要，完整明细追加到运行目录 `logs/trail-api-timing.jsonl`；每行还包含整轮真实 SDK `request_count`、`api_total_ms`、`client_build_count`、包含初始化的 `observed_total_ms` 和 `run_elapsed_ms`，便于区分 API 等待与本地计算，并直接定位最慢的币种和阶段。缓存命中、`build_clients` 初始化和 `_slippage_price` 等本地辅助方法不计入 `request_count`，但初始化耗时仍单列并参与最慢调用排名。
- P1 因 action limit 没有执行时，worker 只记录 `action_limit_deferred_at` / `last_error`，保留原 `status` 和 `oid`，不会再把订单改成 `paused_action_limit` 或 `paused_action_rate_limit`；历史保存的旧状态仍按兼容逻辑识别。
- paused 档位恢复提交前会先用当前 best bid/ask（读取失败时用 mid）判断限价是否会立即成交；会穿盘口的 paused 单只本轮延后恢复，不提交给交易所，避免反复触发 ALO 拒单。
- 同一轮同一侧的即时 replacement 不限制张数，会处理全部待补成交；成功提交后该侧本轮不再提交其他类型的新单，后续普通补档或恢复由下一轮继续维护。

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
- 模式、仓位范围或最低下单额变化后，如果现有订单违反新的方向、仓位上下限、withdrawable 保护或 reduce-only 要求，Worker 仍会按安全规则单独撤销相关订单。
- 从 `avg` 模式切回无方向的普通网格可使用 `--modify --trend 0`。
- `--modify` 只改变命令中明确提供的参数；例如只传 `--trend` 时会沿用原来的 gap，只传 `--gap` 时也会沿用原来的 trend。`--modify --gap 0` 会按当前价格和费率重算默认 gap。
- 修改 `--limit` 的下限后，Worker 会按新仓位范围维护后续订单；`withdrawable < 5` 时账户保护仍优先，Worker 不会为了达到下限而新增风险。

### 查询、恢复、取消

```bash
BTC grid --recover --limit -300 300 --gap 0.5%
BTC grid --query
BTC --cancel grid
```

- `BTC grid --recover --limit -300 300 --gap 0.5%` 会从当前该币普通 limit open orders 里按近侧最多每边 16 张接管到 `server_batch.json`，用于服务器断点或 JSON 丢失后的人工恢复。
- `BTC grid --query` 会展示该币 grid 的 limit/min/gap/仓位、买卖两边 active 数量、每张子单的 oid/价格/状态/live 情况和最近成交。
- `BTC --cancel grid` 会取消服务器维护的网格和所有活跃子单。

### Worker 行为

- 到达持仓上下限后，worker 会压缩越界方向的 grid 单，但最靠近盘口的一张作为保活单继续保留/恢复；因此实际持仓最多可能越过配置边界一个子单金额，保活单成交后不会继续叠加第二张越界单。
- 仓位降到能容纳下一张加仓单后，worker 再把加仓方向补回到最多 16 张。
- 补缺失子单时优先参考盘口 best bid/ask，而不是只参考 mid；盘口读取失败时才退回 mid。
- 加风险方向 active 单超过当前风险密度预算时，worker 会按 `avg_multiplier` 把允许数量压缩为 `floor(16 / multiplier)`，最低保留 1 张；超过部分从最远档开始暂停为 `paused_risk_density`，始终优先保留最近档。系数下降后，`paused_risk_density` 会按由近到远的顺序优先于常规补档恢复。
- 持仓 ROE 由 Hyperliquid `position.returnOnEquity` 直接返回；低于 -10% 时，worker 会按 -10% 到 -40% 的线性区间压缩加仓侧 active 数量，超出部分暂停为 `paused_roe`；低于 -40% 时，加仓侧仍最低保留 1 张保活单。强制减仓仍只由 `panic_ratio` 触发。
- 为减少挂单保证金占用并避免 1 分钟内盘口被打穿，每侧 active grid 单最多保留 16 张；普通补档、缺单恢复和 paused 恢复都要求该侧 active 少于 16 张。成交反向单优先提交，提交后若超过 16 张，优先保留成交反向单，并从其他普通旧 active 的最远档开始暂停为 `paused_active_cap`；之后 active 少于 16 张时，从最近的 paused 档开始逐步恢复。即时 replacement 即使单侧 actual open active 已超过 32 张也仍先提交，随后 worker 优先从最远侧 live `pending_cancel` 开始撤普通旧单，逐步把 active 压回 32 张以内；历史 replacement 恢复仍需等 active 回到 32 张及以下。
- P2 额度充足时，worker 会把同侧 active 与所有当前可恢复的 paused 档位一起按离盘口由近到远排序，每轮每侧最多做一组 `active <-> paused` 渐进式互换：固定恢复最近的合格 paused，并撤销比它更远的最远 active，不再使用对数抽样。P2 撤销目标 active 并提升该最近 paused 的恢复优先级，实际重新挂单仍由下一轮 P1 执行。一换一互换只要不增加当前加风险单数量，就不会因 `avg_multiplier` 已把风险密度预算压到当前数量以下而被阻断；新增风险单仍受该预算限制。会立即穿过盘口或仍受仓位上下限、ROE、withdrawable、reduce-only 容量约束的 paused 档位不参与本轮互换。
- 为避免异常循环无限堆积可恢复记录，`levels` 内同侧 active、pending、recovery_deferred 和 paused 合计最多保留 1024 张；历史记录不占这个名额，仍由独立历史裁剪控制。超过时会优先清理普通 paused，必要时先撤交易所 active 挂单再从本地清除，`replacement_order` 最后才会被清。
- 全仓或逐仓的加仓方向若被交易所以保证金不足拒绝，worker 会对该方向冷却 10 分钟，本轮不再继续试单；减仓方向照常维护。仓位缩小会提前解除冷却，否则到期后探测一次。
- reduce-only 子单的活动数量总和不会超过当前可减仓数量；`withdrawable < 5` 保护期间计算可恢复容量时还会计入同方向已有的普通减仓单，避免普通单与新恢复的 reduce-only 合计超过实际持仓。交易所因可减仓数量不足自动取消的 `reduceOnlyCanceled` 不会被当作手动撤单反复补回。
- 单边漂移接近爆仓时会主动泄压：空仓用 `(liqPx - mid) / (mid - 最近active买入减仓价)`，多仓用 `(mid - liqPx) / (最近active卖出减仓价 - mid)` 计算 `panic_ratio`；若低于 `panic_ratio_threshold`（默认 `100`），Worker 会先提交 IOC + 滑点保护的 reduce-only panic 单，确认成交后再以回执中的实际成交均价 `avgPx` 为锚点，在反方向 `2 * gap` 处提交实际成交 size 的 GTC 回补单；只有回执缺少有效 `avgPx` 时才回退到触发时 mid。这个顺序比旧的同批 `bulk_orders` 多一次 API 往返，但不会在 IOC 结果未知时预挂无支撑的回补单，也不再需要按部分成交量二次修改已挂 GTC。回补提交失败会按实际减仓成交量和首次计算价格持续重试。这张 panic 回补单保持 `status=active` 并带 `replace_never_cancel=true`：除非成交或用户明确取消整条 grid，否则不会被 ROE、风险密度、仓位范围、active cap、reduce-only 刷新、dense regrid、P2 近远重排、连续加仓刹车或历史裁剪撤销；需要压缩 active 数量时普通单先让位。它从首次提交开始就固定使用成交锚定价格，不参与 active/paused 价格占位检查，且不会因最小名义金额重试而放大 size。panic 减仓本身不作为普通 fill 近距离回补，永久回补单成交后生成的下一张普通反向单不继承 `replace_never_cancel`，恢复为一般 grid OID。
- Worker 使用账户 USDC `withdrawable` 作为增加风险的资金保护：`withdrawable < 5` 时，加风险方向只允许最低 1 张保活单，减风险方向强制使用 reduce-only。此时减风险方向的所有 paused 类型（包括 `paused_withdrawable`、replacement、limit、margin、reduce capacity、ROE、risk density、active cap 和 action limit）都可按近处优先参与恢复，不等待余额回到 `10`；恢复单绕过原暂停原因中的仓位下界和保证金冷却，但仍受当前盘口不穿价、单侧 active 上限、每方向每轮最多一张、请求额度和实际可减仓数量约束。USDC 可提余额低于 `10` 期间，Worker 每轮仍会在同一账户的全部 grid 中按订单 `timestamp` 从早到晚选择一张 `active`、非 reduce-only、非永久保护单；旧记录缺少交易所毫秒 `timestamp` 时使用 `submitted_at * 1000`，同一毫秒才以数值 `oid` 从小到大打破并列。选中单正常暂停为 `paused_withdrawable`，每个账户每轮最多一张；`5 <= withdrawable < 10` 时在 P2 执行，`0 < withdrawable < 5` 时提级到 P1 尾部执行，`withdrawable = 0` 时提级到 P0 执行。P2 撤单继续受 P2 headroom 门槛约束，P1 尾部撤单使用 P1 请求预算，P0 撤单不等待 P2 headroom。`5 <= withdrawable < 10` 时 paused 恢复仍等待余额回到 `10`。爆仓风险独立由 `panic_ratio` 监控和主动减仓。交易所实际返回保证金不足后仍进入同侧冷却。保护解除后，Worker 按当时盘口重新计算缺失档位，并保持每轮每方向最多新增一张，不恢复保护期间的旧价格。
- 普通成交产生的即时 replacement 在生成当轮绕过 `limit`、ROE、withdrawable、active cap、本地 action limit 和每方向提交前置检查，优先尝试把反向单挂出；成功后仍占用该方向本轮额度，交易所真实拒单也照常处理。下一轮起重新纳入全部控制：不满足条件时正常 pause，满足保活条件时最多留下 1 张。
- grid/trail 创建、修改和取消与 Worker 共用 `server_batch.lock`；命令执行期间 Worker 会跳过该轮，避免旧任务快照覆盖刚完成的修改，不需要手动暂停定时器。
- 旧版保存的非 ALO grid 子单在 Worker 再次提交时会自动刷新为 ALO；历史 post-only 拒绝仍按可恢复状态兼容处理。

### 接续和边界

- worker 每次至少回看最近 24 小时成交记录；如果 `last_fill_check_ms` 更早，会从更早的断点继续查。
- worker 只维护 `server_batch.json` 里记录的 oid；手动下的新单不会被自动接管。
- 如果 worker 发现 grid oid 消失但最近成交记录里找不到对应 fill，会按该记录原来的 side、price、size 重新挂回，并在任务 note 里记录 `recovered_missing`。
- grid oid 从 open orders 消失而成交查询尚未更新时，worker 会再查订单状态；已 `filled` 的直接补对向单。若恢复挂单因保证金保护等条件暂缓，会保留原 OID 供下一轮继续核实，避免漏掉稍后才可见的成交。
- 恢复历史暂停档位前会检查当前 best bid/ask；已经穿过盘口的旧价格不再恢复，下一轮改按当前盘口重新补档，避免旧档批量立即成交。
- 普通 paused 档位会按 side、price、size 和 reduce-only 去重；每侧只保留填补当前活跃档位缺口所需的最新记录。成交反向单产生的 paused replacement 不受每侧 16 张目标限制，会一直保留等待恢复。其他 grid 历史仍最多保留最近 120 条。
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
