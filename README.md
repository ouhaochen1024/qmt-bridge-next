# qmt-bridge-next

大 QMT（迅投客户端内置 Python）信箱桥接 —— 外部 HTTP/WS API 服务。

**配套 Skill 仓库**：[ouhaochen1024/qmt-trading-next-skill](https://github.com/ouhaochen1024/qmt-trading-next-skill)（在 Cursor / Claude Code / DeepSeek Harness / 豆包工作 / WorkBuddy 中用自然语言完成交易、当日盈亏、复盘与飞书同步）。

与旧 qmt-bridge（miniQMT 基座，`import xtquant` 直连）的区别：

- **不再 import xtquant**，不再需要 miniQMT / XtQuantServer 权限；
- **交易与账户查询**经文件信箱协议进入大 QMT 内置策略
  `mailbox/qmt_mailbox_executor.py`（passorder / cancel /
  get_trade_detail_data）——实盘验收通过；
- **行情快照 / K 线 / 交易日历 / 股票名称**由桥接的**公开行情源**直接获取
  （腾讯主源 + 东方财富兜底），不经过 QMT；
- 板块/财务/公式等其余能力显式返回 501 unsupported（见
  `docs/limitations.md`），不假装成功。

## 架构

```
Skill / 前端 ──HTTP/WS──▶ bridge（本仓库外部端）
        │                     │  MailboxClient（唯一信箱入口）
        │                     ▼
        │               MAILBOX/in|out（JSON 帧，原子写）
        │                     │
        │                     ▼
        │         大 QMT 内置策略 mailbox/qmt_mailbox_executor.py
        │         （passorder · cancel · get_trade_detail_data）
        │
        └──▶ 行情请求 ──▶ 公开行情源（腾讯 qt.gtimg.cn / 东方财富 push2）
```

- Skill 层（qmt-trading-next-skill）只依赖 HTTP/WS 合同，
  不读写信箱目录、不依赖信箱路径。
- 桥接端不决定账户 id：`account_id` 来自统一配置（运维显式声明），
  请求里为空就透传；未绑定时执行器报 `account not bound`，桥接转 409。

## 统一配置（唯一配置源）

`config/config.ini` —— 桥接 / 执行器 / Skills 三端共用的**唯一配置源**：

```ini
host               = 0.0.0.0                       ; 桥接 HTTP 监听
port               = 8000
api_key            = <随机生成的密钥>               ; Skill 端 X-API-Key 须一致
account_id         = <资金账号>                     ; 执行器启动时 set_account
credit_account_id  = <信用两融账号>                 ; 可选，双账户复盘用
default_account    = stock                          ; 可选，stock / credit
base_dir           = D:\...\qmt-bridge-next\mailbox ; 信箱目录（全部产物收敛于此）
```

- 模板见 `config/config.example.ini`；`config.ini` 本体已被 gitignore，
  **真实账号与密钥不会进入 git 历史**。
- **鉴权**：配置 `api_key` 后，`/api/trading/*` 与 `/account/*` 必须携带
  `X-API-Key`（常量时间比较，缺失/错误返回 401）；行情、日历、健康检查
  保持公开。`api_key` 为空时启动日志会打印告警。
- **默认监听 `127.0.0.1`**：只有显式写 `host = 0.0.0.0` 才会暴露到局域网。
- 查找顺序：`$QMT_CONFIG` → `<仓库根>\config\config.ini` → `cwd\config\`；
  单键可用环境变量 `QMT_BRIDGE_*` 临时覆盖。
- 修改 `base_dir` / `account_id` 后需重跑盖戳脚本并重贴执行器（见下）。

## 快速开始

### 本地模拟（无 QMT）

```bash
pip install -e ".[dev]"
python devtools/mock_executor.py --mailbox ./smoke/mailbox        # 终端 1
QMT_BRIDGE_MAILBOX=./smoke/mailbox python -m qmt_bridge           # 终端 2，默认 :8000
curl http://127.0.0.1:8000/health
```

执行器未启动时 `/health` 返回 503 degraded，错误含
`executor not alive`；mock 启动后自动转 healthy。

### 连接真实大 QMT

1. 编辑 `config/config.ini`（账号、base_dir、api_key）；
2. 盖戳：`bash mailbox/stamp_executor.sh`（把 base_dir 与 config.ini
   绝对路径写进执行器常量 BASE_DIR / CONFIG_INI）；
3. 把盖戳后的 `mailbox/qmt_mailbox_executor.py` 全文粘贴进 QMT 策略
   （运行周期 1 秒，运行设置里绑定交易账户），重启；
4. 启动桥接：`python -m qmt_bridge`（控制台 + `mailbox/logs/bridge-YYYYMMDD.log`）；
5. 收紧信箱 ACL（一次性，必做）：
   `icacls "<base_dir>" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"`

控制台确认三行即部署成功：

```
[qmt-exec] === init begin build=2026-10-27-mbx10 ===
[qmt-exec] paths resolved: ... mailbox=<base_dir> ...
[qmt-exec] account id loaded from config.ini: <资金账号>
```

## 主要能力

| 能力 | 路由 | 后端 |
| --- | --- | --- |
| 下单 / 撤单（两阶段） | POST `/api/trading/order` `/cancel` 等 | 信箱执行器 |
| 资产 / 持仓 / 委托 / 成交查询 | GET `/api/trading/*` `/account/*` | 信箱执行器 |
| 订单确认事件推送 | WS `/ws/order` `/ws/events` | 信箱执行器 |
| 实时快照（含五档盘口、名称） | GET `/api/market/full_tick` `/indices` | 公开行情源 |
| K 线（1m~1d，复权） | GET `/api/market/market_data_ex` `/history` | 公开行情源 |
| 交易日历 | GET `/api/calendar/*` | 公开行情源 |
| 板块 / 财务 / 公式 / ETF / 期权等 | — | 501 unsupported（见 limitations.md） |

## 下单两阶段语义

- `POST /api/trading/order` 返回 200 + `data.status`：
  - `confirmed`：order_id 已知；
  - `submitted`：`order_id=0` 是合法状态，凭 `tag` 追踪；
- 确认结果经 WS `/ws/order` 推送 `order_confirmed` /
  `order_confirm_timeout`；Broker 会回放最近事件，但**不保证不丢**——
  没收到确认时用 `GET /api/trading/orders` 对账，不要重新下单；
- 收到 timeout 时**报告"已提交但未确认，请人工检查"**，
  绝不重新下单（幂等缓存 `mailbox/processed.json` 跨执行器重启有效，
  人工重下才是重复执行风险）。

## 开发与测试

```bash
python -m pytest tests/ -q     # 14 项：信箱回路 + 桥接 HTTP/WS 集成
python devtools/probe_minimal.py --mailbox ./smoke/mailbox   # 最小协议验证
python devtools/mock_executor.py   # 无 QMT 环境的执行器模拟器
```

## 已知限制

见 `docs/limitations.md`（含公开行情源的时效性与限流说明）。
