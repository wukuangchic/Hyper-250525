# Hyperliquid 本地下单工具

这是一个基于官方 `hyperliquid-python-sdk` 的本地轻量下单工具。日常建议双击 `order-terminal.command` 进入专用 Terminal，也可以在普通终端里使用 `order` alias。

## 命令速记

下单命令从前到后可以记成 6 段：

1. `coin`，例如 `BTC`
2. `buy/sell`，或 `query`
3. `amount`，默认 `10`
4. `entry/exec` 选项，常用的是 `--market`、`--price`、`--stop`、`--stop-limit`、`--take`、`--take-limit`、`--level`、`--tif`、`--slippage`
5. `tp/sl`，就是 `--tp`、`--sl`
6. `--reduce-only`

其中：

- `--stop` / `--take` 是入场触发价。`--stop` 偏突破，`--take` 偏触价。
- 你可以直接写成 `70000+50`、`70000-50`、`70000+0.2%` 这种 `触发价+偏移` 形式；不写偏移时默认是触发市价单。`--stop-limit` / `--take-limit` 也仍然可用，作为显式写法。
- 百分比是按触发价计算的，所以 `70000+2% = 71400`，如果你想要 `70140`，写 `70000+0.2%`。
- `--tp` / `--sl` 也支持 `ABS+OFFSET` 或 `REL%+OFFSET` 这种写法；相对百分比是按入场价或持仓均价计算的。买多通常用正百分比止盈、负百分比止损，卖空则相反。
- 这类百分比算出来的触发价和限价，会先对齐到交易所可接受的价格精度，再提交下单。
- 这种带触发的偏移限价不是 `ALO`；它们是 trigger-limit。只有普通限价单才会走 `--tif`。
- `--tp` / `--sl` 是止盈止损；不加 `--reduce-only` 时是 bracket，加了 `--reduce-only` 时是保护已有仓位。
- 不写价格时，限价单默认按同向订单簿第 `10` 档挂单，且默认 `ALO`。`--level` 是主写法，`--book-level` 仍然保留作兼容别名。

当前 SDK 信息：

- SDK：`0.23.0`
- commit：`7ee976d123b1e04295e4a1e37a424ca6a13bef88`
- 官方仓库：https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- 官方 API 文档：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api

## 快速开始

首次安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，填入主账户地址和 API wallet / agent 私钥。

双击：

```text
order-terminal.command
```

打开的新 Terminal 会进入下单专用模式，可以省略 `order`：

```bash
query
BTC buy --dry-run
BTC buy
BTC buy 10 --market
GOLD buy
SAMSUNG buy
QQQ buy --dry-run
BTC --cancel
```

普通终端里先加载 alias：

```bash
. ./aliases
query
order BTC buy --dry-run
order BTC buy 10 --market
order BTC --cancel
```

注意 alias 要分两步执行，不要写成 `. ./aliases && order BTC buy`。

## Windows 用法

首次安装时在项目目录里双击：

```text
setup-windows.cmd
```

安装完成后双击：

```text
order-terminal-windows.cmd
```

打开的 CMD 窗口里可以直接运行：

```bat
query
order BTC buy --dry-run
order BTC buy 10 --market
markets BTC
```

也可以在普通 CMD / PowerShell 里进入项目目录后运行：

```bat
order.cmd BTC buy --dry-run
query.cmd
markets.cmd QQQ
```

## Simple-Hyper 手机网页

`simple_hyper_server.py` 是给 iPhone/Safari 用的极简网页控制台。它不会重写交易逻辑，只是在服务器上调用当前目录的 `hl_order.py`。

### 新服务器部署

在任意 Ubuntu 服务器上拉代码、安装依赖：

```bash
git clone git@github.com:wukuangchic/Hyper-250525.git
cd Hyper-250525

sudo apt-get update
sudo apt-get install -y python3-venv git

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

cp simple-hyper.env.example simple-hyper.env
set -a
. ./simple-hyper.env
set +a
python3 simple_hyper_server.py --host 0.0.0.0 --port 8787
```

如果服务器没有配置 GitHub SSH key，可以改用 HTTPS clone：

```bash
git clone https://github.com/wukuangchic/Hyper-250525.git
```

如果 `python3 -m venv .venv` 提示缺少 `ensurepip`，在 Ubuntu 24.04 上安装：

```bash
sudo apt-get install -y python3.12-venv
```

云服务器安全组 / 防火墙需要放行：

```text
TCP 8787
```

手机访问：

```text
http://服务器地址:8787
```

如果设置了 `SIMPLE_HYPER_TLS_CERT` 和 `SIMPLE_HYPER_TLS_KEY`，则改用：

```text
https://服务器地址:8787
```

公网使用时建议启用 HTTPS，否则钱包密钥会以明文 HTTP 请求经过网络。

### systemd 常驻

如果要像服务器 C 一样后台常驻并开机自启，可以创建：

```bash
sudo cp simple-hyper.env.example /etc/simple-hyper.env
sudo nano /etc/simple-hyper.env
```

示例 `/etc/simple-hyper.env`：

```text
SIMPLE_HYPER_HOST=0.0.0.0
SIMPLE_HYPER_PORT=8787
SIMPLE_HYPER_COMMAND_TIMEOUT=60
SIMPLE_HYPER_MAX_COMMAND_LENGTH=240
```

创建服务：

```bash
sudo tee /etc/systemd/system/simple-hyper.service >/dev/null <<'UNIT'
[Unit]
Description=Simple-Hyper mobile web server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Hyper-250525
EnvironmentFile=/etc/simple-hyper.env
ExecStart=/home/ubuntu/Hyper-250525/.venv/bin/python /home/ubuntu/Hyper-250525/simple_hyper_server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now simple-hyper.service
sudo systemctl status simple-hyper.service
```

如果仓库目录不是 `/home/ubuntu/Hyper-250525`，把 `WorkingDirectory` 和 `ExecStart` 改成实际路径。

更新代码后重启：

```bash
git pull
. .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl restart simple-hyper.service
```

### 服务器 C 自动同步

服务器 C 使用 `simple-hyper-sync.timer` 定时从 GitHub `main` 同步代码并重启 `simple-hyper.service`。同步脚本保留服务器本机的 `.venv/`、`logs/` 和 `coin_aliases.csv`，避免覆盖运行环境、历史日志和本机别名表。

检查同步状态：

```bash
systemctl status simple-hyper-sync.timer
journalctl -u simple-hyper-sync.service -n 80 --no-pager
```

### 网页使用

页面流程：

```text
1. 输入 Wallet Address 和 Private Key or Agent Key
2. 点击 Verify
3. 验证成功后地址和密钥输入框会隐藏
4. 用 Query Account 查询账户
5. 在 Command 输入框输入命令
6. 点击 Run 执行
```

页面底部有 `README` 链接，里面有英文命令示例。

安全默认值：

- 服务器不需要保存交易 `.env`。
- 网页验证通过后会隐藏地址和密钥，凭证只留在当前页面内存里。
- 每次执行请求都会带 `account_address` 和 `secret_key`，后端只放进子进程环境变量。
- 后端用 `shlex.split()` 解析输入框命令，不走 shell。
- 命令不含 `--dry-run` 且不是 `query` 时，前端会弹出真实提交确认。
- 服务日志不记录请求 body，`hl_order.py` 日志也不会写入密钥。
- 只输入单个标的，例如 `BTC`，只查询行情、文本 K 线和当前持仓，不会下单。

## 默认行为

- 读取根目录 `.env` 的 `account_address` 和 `secret_key`，也支持同名环境变量覆盖。
- 如果签名地址是 agent，会自动解析到绑定的主账户。
- 默认网络是主网。
- 默认金额是 `10` 美元。
- 默认真实提交下单或撤单；加 `--dry-run` 只预演。
- 不填价格时，按同向订单簿第 `10` 档挂单：
  - 买入 / 看多：第 10 档 bid。
  - 卖出 / 看空：第 10 档 ask。
- 加 `--market` 时，按当前 mid 计算数量，并用带滑点保护的 IOC 单成交，不会留下挂单。
- 加 `--stop-entry`（或更短的 `--stop`）时，会提交 stop-entry；不写偏移时是触发市价单，写成 `70000+50` / `70000+0.2%` 这种形式时会变成触发限价单。
- 加 `--take-entry`（或更短的 `--take`）时，会提交 take-entry；不写偏移时是触发市价单，写成 `70000+50` / `70000+0.2%` 这种形式时会变成触发限价单。
- 加 `--tp` / `--sl` 时，可以写成绝对价、绝对价加偏移，或者相对入场价 / 持仓均价的百分比，再跟一个偏移，比如 `2%+0.1%` 或 `-2%-0.1%`。
- 加 `--tp` / `--sl` 且不加 `--reduce-only` 时，会用 `normalTpsl` 一次提交开仓单和子止盈 / 止损单。
- 加 `--tp` / `--sl` 且同时加 `--reduce-only` 时，会按 `positionTpsl` 提交保护已有仓位的触发单。
- 加 `--scale` 时，会把总金额平均拆成多张限价单；每张子单金额必须至少 `10` 美元。
- 真实下单前，默认把当前合约 cross 杠杆设置为 `maxLeverage`；如果标的不支持 cross，会自动切到 isolated，默认使用 `5x`。
- 如果数量 round 后名义价值低于 `10` 美元，会向上补一个数量步进。
- 前台默认精简输出，完整日志写入 `logs/`。

## 常用命令

```bash
# 预演，不下单
BTC buy --dry-run

# 查询全部持仓和未成订单
query

# 查询 BTC 最近 24 小时走势、文本 K 线、最高点、最低点、成交额；如果有持仓，会显示持仓量、持仓额和杠杆
BTC

# 默认 10 美元看多
BTC buy

# 指定金额
BTC buy 25

# 指定价格
BTC buy 10 --price 75000

# 市价买入 / 卖出
BTC buy 10 --market
BTC sell 10 --market

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

# 调整市价单滑点保护，默认 5%
BTC buy 10 --market --slippage 1%

# 指定同向订单簿档位
BTC sell 10 --level 5

# 只减仓 / 平仓，不允许反手
BTC sell --reduce-only

# 保护已有仓位的止盈 / 止损触发单
BTC sell 25 --tp 72000 --reduce-only
BTC sell 25 --sl 65000 --reduce-only
BTC sell --tp 2%+0.1% --sl -2%-0.1% --reduce-only

# 触发限价单；也可以把限价偏移写进同一个参数里
BTC sell 25 --sl 65000+50 --reduce-only
BTC buy --tp 2%+0.1% --sl -2%-0.1%
BTC sell --tp -2%-0.1% --sl 2%+0.1%

# 开仓同时挂止盈止损
BTC buy 100 --price 68000 --tp 72000 --sl 65000

# 分批挂单，从 67000 到 63000 平均拆 5 笔
BTC buy 100 --scale 5 --from 67000 --to 63000

# 取消 BTC 所有挂单
BTC --cancel

# 取消指定订单
BTC --cancel 441260592983
```

普通终端里加 `order` 前缀：

```bash
order query
order --query
order BTC buy
order BTC buy 10 --market
order BTC --cancel
```

## 标的索引

官方可交易标的以 Hyperliquid `meta` API 返回的名字为准，不以 App URL 为准。比如 App 路由能打开 `xyz:SAMSUNG`，但 API 真实标的是 `xyz:SMSN`。

本地别名统一维护在：

```text
coin_aliases.csv
```

字段：

- `alias`：你日常输入的名字。
- `target`：Hyperliquid API / SDK 实际下单用的标的。
- `note`：备注。
- `rate`：可选换算倍率。前台 `price` 会显示为 `原始报价(原始报价/rate)`。

当前示例：

```csv
alias,target,note,rate
GOLD,xyz:GOLD,Hyperliquid app route uses builder DEX xyz,
SAMSUNG,xyz:SMSN,Samsung Electronics on xyz,
QQQ,xyz:XYZ100,Nasdaq100,41.09
```

因此这些命令都可以用：

```bash
GOLD buy
SAMSUNG buy
QQQ buy
xyz:GOLD buy
xyz:SMSN buy
```

`rate` 适合处理永续合约报价和日常习惯报价不一致的标的。例如 `QQQ -> xyz:XYZ100`，终端会保留原始永续报价，同时在括号里显示除以 `rate` 后的习惯价格。

查询官方标的：

```bash
markets BTC
markets GOLD
markets SAMSUNG
markets QQQ
markets --dex xyz --limit 10
```

也可以直接运行：

```bash
./hl_markets.py BTC
./hl_markets.py --csv > perp_markets.csv
```

市场表字段：

- `asset_id`：永续合约资产 id。
- `dex`：默认主 DEX 或 builder DEX 名称。
- `name`：SDK/API 下单用的合约代码。
- `szDecimals`：数量精度。
- `maxLeverage`：最大杠杆。
- `marginTableId`：保证金表 id。
- `isDelisted`：是否下架。

## 前台输出

前台只显示账户指标和订单核心字段。完整运行日志仍写入 `logs/`，但前台不显示日志路径。数字统一使用千分位并保留 2 位小数。

```text
+- Account ----------------+
| 统一账户比率: 0.19%        |
| 统一账户杠杆: 0.09x        |
+----------------------------+
+- Run --------------------+
| dry_run: 1                 |
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

市价单：

```bash
BTC buy 10 --market
BTC sell 10 --market
BTC buy 10 --market --slippage 1%
```

`--market` 会使用 IOC 订单；默认滑点保护是 `0.05`，也就是 `5%`。如果要更保守，可以用 `--slippage 1%` 或 `--slippage 0.01`。

止盈 / 止损：

```bash
# 保护已有多仓：到价后卖出平仓
BTC sell 25 --tp 72000 --reduce-only
BTC sell 25 --sl 65000 --reduce-only

# 突破后开仓
BTC sell 25 --stop 70000
BTC buy 25 --stop 80000

# 开多时同时挂止盈和止损
BTC buy 100 --price 68000 --tp 72000 --sl 65000

# 子止盈 / 止损默认是触发市价单；指定 limit 就会变成触发限价单
BTC sell 25 --sl 65000 --sl-limit 64800 --reduce-only
BTC buy 100 --price 68000 --tp 72000 --tp-limit 71900 --sl 65000 --sl-limit 64800
```

分批挂单：

```bash
BTC buy 100 --scale 5 --from 67000 --to 63000
BTC sell 200 --scale 4 --from 72000 --to 76000 --reduce-only
```

`--scale` 会把总金额平均分到每个价格；例如 `100` 美元拆 `5` 笔，就是每笔约 `20` 美元。拆分后每笔必须至少 `10` 美元。

查询指令会返回当前所有 DEX 的持仓和未成订单：

```bash
query
order query
order --query
```

其中 `Positions` 显示当前持仓，`Open Orders` 显示当前未成订单。`side=B` 是买入 / 看多挂单，`side=A` 是卖出 / 看空挂单。

## 日志和排错

每次运行都会在这里保存完整日志：

```text
logs/
```

日志包括：

- 命令参数
- 账户上下文
- L2 订单簿
- `user_state`
- `spot_state`
- 统一账户指标计算过程
- 杠杆更新结果
- 下单 / 撤单返回
- 异常 traceback

排错时可以加：

```bash
BTC buy --dry-run --verbose
```

如果出现 `Unknown perp coin`，优先用 `markets` 查真实 API 标的，再把常用缩写写入 `coin_aliases.csv`。

## 统一账户指标

真实下单流程：

```text
1. 解析标的 / 方向 / 金额 / 价格
2. 设置该合约杠杆；cross 用最大杠杆，不支持 cross 时 isolated 用 5x
3. 提交下单
4. 下单接口返回
5. 扫所有 DEX 计算统一账户比率和统一账户杠杆
6. 前台显示精简结果
```

撤单也是撤单返回后再计算指标。`--dry-run` 不提交动作，只显示当前状态。

当前本地口径：

- 统一账户比率：将各 DEX 的 maintenance margin 按 collateral token 聚合，再除以对应 spot 抵押品余额，取风险最高的一组。
- 统一账户杠杆：当前总名义仓位 / 活跃抵押品余额。

## 价格、数量和持仓规则

直接调用 API 时，价格和数量需要是字符串，不能有多余尾随零。

```text
正确："50000", "0.01"
错误："50000.00", "0.010"
```

使用 SDK 下单时可以传 `float`，SDK 会通过 `float_to_wire()` 转成规范字符串；如果传入会造成非法舍入的数值，SDK 会抛 `ValueError`。

精度规则：

- 价格最多 `5` 个有效数字。
- 永续价格小数位最多 `6 - szDecimals`。
- 现货价格小数位最多 `8 - szDecimals`。
- 数量精度由该资产的 `szDecimals` 控制。

参考：

```text
https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/7ee976d123b1e04295e4a1e37a424ca6a13bef88/examples/rounding.py
```

Hyperliquid 同一合约是净仓位逻辑，不会同时存在一条多仓和一条空仓。

```text
reduce_only=False：允许开仓、加仓、减仓、反手
reduce_only=True：只允许减仓/平仓，不允许反手
```

只减仓 / 平仓：

```bash
BTC sell --reduce-only
GOLD sell --reduce-only
```

## 环境和依赖

`.venv/` 是当前目录专用 Python 虚拟环境，由首次安装命令创建。公开仓库不提交 `.venv/` 和本地 SDK 源码目录，依赖统一由 `requirements.txt` 安装并锁定到当前 SDK commit。

```text
hyperliquid-python-sdk
eth_account
requests
websocket-client
msgpack
```

如果依赖损坏，可以删掉 `.venv/` 后重新执行首次安装命令。

`.env` 需要包含：

```text
account_address=0x主账户地址
secret_key=0x私钥或agent私钥
```

注意：

- `secret_key` 可以是主钱包私钥，也可以是已授权的 API wallet / agent 私钥。
- `account_address` 必须是主账户地址，不是 agent 地址。
- 不要把真实私钥提交进 Git。

## SDK 速查

只读查询用 `Info`：

```python
from hyperliquid.info import Info
from hyperliquid.utils import constants

info = Info(constants.MAINNET_API_URL, skip_ws=True)
print(info.all_mids()["BTC"])
```

交易动作使用 `Exchange`：

```python
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

wallet = eth_account.Account.from_key("0x你的私钥")
exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address="0x主账户地址")
```

当前 SDK 不是 `Exchange(address, private_key, url)` 这种接口。

常用 `Info` 方法：

- `info.all_mids()`
- `info.meta()`
- `info.meta(dex="xyz")`
- `info.spot_meta()`
- `info.user_state(address)`
- `info.user_state(address, dex="xyz")`
- `info.spot_user_state(address)`
- `info.open_orders(address)`
- `info.open_orders(address, dex="xyz")`
- `info.l2_snapshot("BTC")`
- `info.l2_snapshot("xyz:GOLD")`

## 签名和限制

优先让 SDK 自动签名。

两类签名：

1. 交易类 L1 action，例如下单、撤单、改单、杠杆调整。
   - SDK 会构造 action hash，再签 phantom agent。
   - EIP-712 domain 的 `chainId` 是 `1337`。
   - mainnet/testnet 通过 phantom agent 的 `source` 区分。

2. user-signed action，例如 USD 转账、spot 转账、提现、staking delegation。
   - SDK 会写入 `signatureChainId` 和 `hyperliquidChain`。
   - 当前 SDK 默认 `signatureChainId` 是 `0x66eee`。
   - 一些官方/前端兼容示例里会看到 Arbitrum `0xa4b1`。

速率限制常见值：

- REST：每 IP 聚合权重约 `1200/min`。
- WebSocket：每 IP 最多 `10` 个连接。
- WebSocket：每 IP 最多 `1000` 个订阅。
- WebSocket：每分钟最多 `30` 个新连接。

官方说明：

```text
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
```
