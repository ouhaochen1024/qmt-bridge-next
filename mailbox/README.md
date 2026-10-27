# mailbox/ — 大 QMT 内部端（信箱执行器）

`qmt_mailbox_executor.py` 运行在**大 QMT 客户端内置 Python**里，是信箱协议的
执行端。协议字段、方法名**不得修改**；如需扩展方法只能追加。

## 部署（mbx10 约定：config\config.ini 是唯一配置源）

统一配置在 `<仓库根>\config\config.ini`（含 account_id / base_dir /
host / port / api_key），**三端共用的唯一配置源**：

```ini
account_id = 66660000      ; 资金账号（执行器启动时 set_account）
base_dir   = <信箱目录>     ; 执行器/桥接共用的信箱目录（默认即本目录）
host / port / api_key      ; 桥接 HTTP 监听与密钥
```

部署流程：

1. 编辑 `config\config.ini`（改账号、base_dir 或监听配置）；
2. 运行盖戳脚本：`bash stamp_executor.sh`（本目录内）
   —— 把 base_dir 写进 `qmt_mailbox_executor.py` 的 `BASE_DIR` 常量
   （幂等，只动这一行）；
3. 把盖戳后的 `qmt_mailbox_executor.py` 全文粘贴进 QMT 策略（运行周期
   **1 秒**，运行设置里绑定交易账户），重启后控制台确认：
   `[qmt-exec] === init begin build=2026-10-27-mbx11 ===`
   `paths resolved: ... mailbox=<base_dir> ...`
   `[qmt-exec] account id loaded from config.ini: <资金账号>`
4. 桥接**无需任何环境变量**：启动即读 `config\config.ini`
   （base_dir 定位信箱；host/port/api_key 生效）。

收紧信箱 ACL（一次性，必做——信箱是唯一指令通道）：

```bat
icacls "<base_dir>" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
```

## 目录布局（全部产物收敛在 base_dir 内）

```
<base_dir>/
  in/  out/  bad/          指令通道（帧）
  hello.json               执行器宣告（含绑定的 account_id、build）
  agent.hb                 心跳（~2s 一次；超 5s 未刷新即视为失联）
  agent.stopped            执行器已停止的标志
  stamp_executor.sh        盖戳脚本（把统一配置写进执行器常量）
  qmt_mailbox_executor.py  盖戳后的执行器（贴进 QMT 的那份）
  logs/                    执行器 + 桥接日志（bridge-YYYYMMDD.log）
```

目录边界 = ACL 边界：收紧一条 icacls 即覆盖指令通道、配置与日志。
兼容性：旧布局 account.ini 仍可被读取，
统一配置（CONFIG_INI 盖戳）优先。

## 已支持的信箱方法（执行器 _HANDLERS，勿改名）

| 方法 | 说明 |
| --- | --- |
| order_stock / order_stock_async | 下单，两阶段（submitted → order_confirmed / order_confirm_timeout 事件） |
| cancel_order_stock(_async) | 撤单（按 order_id/ref/sysid 匹配） |
| cancel_order_stock_sysid(_async) | 按柜台系统编号撤单 |
| query_stock_orders / query_stock_order | 当日委托 / 单笔查询 |
| query_stock_positions | 持仓 |
| query_stock_asset | 资产 |
| query_stock_trades | 当日成交 |

行情快照/K线/交易日历由桥接的公开行情源提供（见 `../docs/limitations.md`）；
板块、财务、公式等仍为 501 unsupported，不要为补功能改执行器。
