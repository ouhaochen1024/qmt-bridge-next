# qmt-bridge-next

大 QMT（迅投客户端内置 Python）信箱桥接 —— 外部 HTTP/WS API 服务。

与旧 qmt-bridge（miniQMT 基座，`import xtquant` 直连）的区别：

- **不再 import xtquant**，不再需要 miniQMT / XtQuantServer 权限；
- 交易与账户查询经**文件信箱协议**进入大 QMT 内置策略
  `mailbox/qmt_mailbox_executor.py`（passorder / cancel /
  get_trade_detail_data）；
- 行情/板块/财务等 xtdata 能力**不在信箱协议内**，对应路由显式返回
  501 unsupported（见 `docs/limitations.md`），不假装成功。

## 架构

```
Skill / 前端 ──HTTP/WS──▶ bridge（本仓库外部端）
                             │  MailboxClient（唯一信箱入口）
                             ▼
                       MAILBOX/in|out（JSON 帧，原子写）
                             │
                             ▼
                 大 QMT 内置策略 mailbox/qmt_mailbox_executor.py
                 （passorder · cancel · get_trade_detail_data）
```

- Skill 层（qmt-trading-next-skill）只依赖 HTTP/WS 合同，
  不读写信箱目录、不依赖信箱路径。
- 桥接端不决定账户 id：请求里的 account_id 为空就透传，
  执行器用策略上下文绑定的账户；未绑定时执行器报
  `account not bound`，桥接转 409。

## 快速开始（本地模拟，无 QMT）

```bash
pip install -e ".[dev]"
python devtools/mock_executor.py --mailbox ./smoke/mailbox   # 终端 1
QMT_BRIDGE_MAILBOX=./smoke/mailbox python -m qmt_bridge_next # 终端 2，默认 :8000
curl http://127.0.0.1:8000/health
```

执行器未启动时 `/health` 返回 503 degraded，错误含
`executor not alive`；mock 启动后自动转 healthy。

## 连接真实大 QMT

见 `mailbox/README.md`（执行器部署、icacls ACL、run_time、账户绑定）。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| QMT_BRIDGE_HOST / PORT | 0.0.0.0 / 8000 | HTTP 监听 |
| QMT_BRIDGE_API_KEY | 空 | 配置后写操作须带 X-API-Key |
| QMT_BRIDGE_MAILBOX | ./mailbox | 信箱目录（与执行器一致） |
| QMT_BRIDGE_HOME | 空 | 或指定根目录（信箱 = <root>/mailbox） |
| QMT_BRIDGE_ORDER_WRITES_ENABLED | true | 写门禁总开关 |
| QMT_BRIDGE_EXECUTOR_HB_MAX_AGE_SEC | 5 | agent.hb 超龄判定 |

## 下单两阶段语义

- `POST /api/trading/order` 返回 200 + `data.status`：
  - `confirmed`：order_id 已知；
  - `submitted`：`order_id=0` 是合法状态，凭 `tag` 追踪；
- 确认结果经 WS `/ws/order` 推送 `order_confirmed` /
  `order_confirm_timeout`；
- 收到 timeout 时**报告"已提交但未确认，请人工检查"**，
  绝不重新下单（同一请求 id 在执行器侧幂等，人工重下才是重复执行风险）。

## 开发与测试

```bash
python -m pytest tests/ -q     # 14 项：信箱回路 + 桥接 HTTP/WS 集成
python devtools/probe_minimal.py --mailbox ./smoke/mailbox   # 最小协议验证
```

## 已知限制

见 `docs/limitations.md`。
