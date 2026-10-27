# limitations.md — 能力清单与限制

> 本构建（qmt-bridge-next）的执行端是大 QMT 内置策略
> `mailbox/qmt_mailbox_executor.py`（只负责**账户与交易**）。
> 行情数据由桥接进程直接从**公开行情源**获取（腾讯行情为主、东方财富兜底），
> 响应形状与旧版 xtdata 一致，Skill 层无感切换。
> 其余未覆盖能力显式返回 **501 unsupported**，绝不假装成功。

## 已支持 —— 信箱执行器（账户与交易）

| 路由 | 信箱方法 |
| --- | --- |
| POST /api/trading/order · /order_async | order_stock（两阶段：submitted → WS order_confirmed / order_confirm_timeout） |
| POST /api/trading/cancel · /cancel_async | cancel_order_stock |
| POST /api/trading/cancel_by_sysid · _async | cancel_order_stock_sysid |
| POST /api/trading/batch_order · /batch_cancel | 逐笔 order_stock / cancel_order_stock |
| GET /api/trading/orders · /order/{id} · /order_detail · /positions · /asset · /trades · /position/{code} | query_stock_orders / query_stock_order / query_stock_positions / query_stock_asset / query_stock_trades |
| GET /account/asset · /positions · /orders · /trades · /binding | 同上（别名路由） |
| GET /health · /executor/status · /api/meta/health | hello.json / agent.hb / agent.stopped 探测 |
| WS /ws/order · /ws/events | 执行器事件帧推送 |

## 已支持 —— 公开行情源（腾讯/东方财富，非 QMT 源）

| 路由 | 说明 |
| --- | --- |
| GET /api/market/full_tick · /api/full_tick（legacy） | 实时快照（含名称、五档） |
| GET /api/market/indices | 七大指数快照 |
| GET /api/market/market_data_ex · /api/market/market_data | K 线（1m~1d，前/后复权），EM 主源 + 腾讯兜底 |
| GET /api/history（legacy） | 单股 K 线 |
| GET /api/calendar/trading_dates · is_trading_date · prev/next_trading_date · trading_period | 交易日历（由指数日 K 推导，缓存 1h） |
| GET /api/utility/batch_stock_name | 批量股票名称 |

公开源限制：非官方接口，字段可能变更；休市时段返回最近收盘数据；
请求过频会被上游限流（桥接内置重试与缓存缓解）。

## 501 unsupported —— 尚未覆盖

| 路由组 | 说明 |
| --- | --- |
| /api/sector/* | 板块列表与成分 |
| /api/financial/* · /api/download/* | 财务数据及其下载 |
| /api/etf/* · /api/cb/* · /api/option/* · /api/futures/* · /api/hk/* · /api/smt/* | ETF/可转债列表与筛选、期权链、期货、港股通、两融标的 |
| /api/instrument/* | 合约信息（涨跌停价等；Skill 若需可用公开源扩展） |
| /api/formula/* | 技术指标公式引擎 |
| /api/meta/*（除 /api/meta/health） | 服务元信息 |
| /api/tick/* · /api/tabular/* · /api/utility/*（除 batch_stock_name） | L2/分笔、表格、工具类 |
| /api/bank/* /api/credit/* /api/fund/* | 银证/两融/基金申赎（信箱执行器仅支持 STOCK 基础交易） |
| /api/trading/ 内的 history_trades · history_orders · new_purchase_limit · ipo_data · com_fund · com_position · export_data · query_data · sync_transaction | xttrader 专有端点 |
| WS /ws/realtime · /ws/whole_quote · /ws/download_progress · /ws/formula · /ws/l2_thousand · /ws/trade | 实时推送类（订单事件请用 /ws/order、/ws/events） |

## 受影响的 Skill 状态

- **完整可用**：qmt-bridge-setup / trading / order-ops / market-watch /
  event-calendar / portfolio-risk / daily-pnl / return-analysis /
  execution-review（复盘部分）/ rebalance / kline-backfill /
  smart-execution（预览部分；涨跌停校验因公开源无该字段而跳过）
- **事件可用、行情推送不可用（4501）**：realtime-monitor
  （`/ws/order`、`/ws/events` 可用；实时价请轮询 full_tick）
- **仍降级（501 明确报错）**：convertible / etf / option / sector-theme /
  fundamental-screen / financial-download / hk-connect /
  technical-signal / credit-margin

## 真实大 QMT 环境验收（需人工执行）

1. 粘贴执行器到 QMT 策略，绑定账户，运行周期 1 秒；
2. 控制台出现 `[qmt-exec] === init begin build=2026-10-27-mbx12 ===`（以执行器 BUILD 常量为准）；
3. 启动桥接后 /health 变 healthy；
4. `query_stock_asset` 返回真实资产；
5. `order_stock` 返回 submitted 后，最终收到 order_confirmed 或
   order_confirm_timeout（WS /ws/order）。
