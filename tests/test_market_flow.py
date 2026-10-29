"""market_flow 解析单测（离线，不依赖网络）。

锁定三个回归点：
1. 涨跌分布的 ±11 桶必须映射成涨停/跌停（曾有实现把它们算进普通涨跌）；
2. 板块 `zgb="21/33"` 是「上涨家数/成分总数」，跌家数要反算；
3. 北向净额必须显式为 None + available=False（2024-08-19 起停止披露），
   任何情况下都不许填估算值。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qmt_bridge import market_flow as mf  # noqa: E402


def _fenbu_page() -> dict:
    return {"data": {"qdate": 20261029, "fenbu": [
        {"-11": 41}, {"-5": 100}, {"-1": 200}, {"0": 56},
        {"1": 200}, {"5": 30}, {"11": 39},
    ]}}


def test_breadth_limit_buckets_and_totals(monkeypatch):
    monkeypatch.setattr(mf.mp, "_get_json", lambda url, **kw: _fenbu_page())
    monkeypatch.setattr(mf, "_two_market_amount", lambda: 15565.24)

    b = mf.get_breadth()
    assert b["limit_up"] == 39
    assert b["limit_down"] == 41
    assert b["up"] == 269          # 200 + 30 + 39
    assert b["down"] == 341         # 41 + 100 + 200
    assert b["flat"] == 56
    assert b["total"] == 666
    assert b["up_ge_5"] == 30       # 不含涨停桶
    assert b["turnover_yi"] == 15565.24
    assert [d["bucket"] for d in b["distribution"]][0] == -11


def test_breadth_falls_back_to_tencent(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("eastmoney blocked")

    monkeypatch.setattr(mf.mp, "_get_json", boom)
    monkeypatch.setattr(mf, "_two_market_amount", lambda: None)
    monkeypatch.setattr(mf, "get_boards", lambda bt: [
        {"up_count": 20, "member_count": 30},
        {"up_count": 5, "member_count": 10},
    ])

    b = mf.get_breadth()
    assert b["source"] == "tencent:board_summary"
    assert (b["up"], b["down"], b["total"]) == (25, 15, 40)
    # 腾讯源没有涨停统计，必须是 None 而不是 0
    assert b["limit_up"] is None


def _board_row() -> dict:
    return {
        "code": "pt01801950", "name": "煤炭", "zdf": "2.21", "zd": "72.41",
        "zljlr": "-9275.23", "turnover": "1027247", "volume": "12112747.00",
        "hsl": "1.09", "lb": "1.16", "ltsz": "18862.52", "speed": "0.12",
        "zdf_d5": "2.36", "zdf_d20": "0.81", "zdf_d60": "5.62",
        "zdf_w52": "12.80", "zdf_y": "21.79", "zgb": "21/33",
        "lzg": {"code": "sh600997", "name": "开滦股份",
                "zd": "0.49", "zdf": "10.00", "zxj": "5.39"},
    }


def test_norm_board_counts_and_leader(monkeypatch):
    b = mf._norm_board(_board_row(), "hy")
    assert b["up_count"] == 21
    assert b["down_count"] == 12      # 33 - 21
    assert b["member_count"] == 33
    assert b["pct_chg"] == 2.21
    assert b["main_net_inflow_wan"] == -9275.23
    assert b["leader"]["code"] == "600997.SH"   # sh600997 → 600997.SH
    assert b["leader"]["pct_chg"] == 10.0


def test_sector_rank_sorts_locally(monkeypatch):
    rows = [
        mf._norm_board(_board_row(), "hy"),
        mf._norm_board(dict(_board_row(), code="pt2", name="银行",
                            zdf="1.97", zljlr="8118.23"), "hy"),
    ]
    monkeypatch.setattr(mf, "get_boards", lambda bt: rows)
    ranked = mf.get_sector_rank("hy", "main_net_inflow_wan", "desc", 1)
    assert ranked["items"][0]["name"] == "银行"   # 8118 > -9275
    assert ranked["count"] == 2


def _mutual_page() -> dict:
    return {"result": {"data": [
        {"MUTUAL_TYPE": "001", "TRADE_DATE": "2026-10-28 00:00:00",
         "DEAL_AMT": 121417.25, "NET_DEAL_AMT": None},
        {"MUTUAL_TYPE": "003", "TRADE_DATE": "2026-10-28 00:00:00",
         "DEAL_AMT": 129976.09, "NET_DEAL_AMT": None},
        {"MUTUAL_TYPE": "005", "TRADE_DATE": "2026-10-28 00:00:00",
         "DEAL_AMT": 251393.34, "NET_DEAL_AMT": None},
    ]}}


def test_northbound_never_fabricates_net_flow(monkeypatch):
    monkeypatch.setattr(mf.mp, "_get_json", lambda url, **kw: _mutual_page())
    nb = mf.get_northbound()

    assert nb["available"] is False
    assert nb["net_inflow_yi"] is None
    assert nb["north"]["net_inflow_yi"] is None
    # 百万元 → 亿元
    assert nb["north"]["turnover_yi"] == 2513.93
    assert nb["north"]["sh_connect_turnover_yi"] == 1214.17
    assert "不提供任何估算" in nb["note"]


def test_ticks_volume_lot_to_share(monkeypatch):
    page = {"data": {"prePrice": 1330.75, "details": [
        "15:00:00,1354.00,3,0,1",
    ]}}
    monkeypatch.setattr(mf.mp, "_get_json", lambda url, **kw: page)
    out = mf.get_ticks("600519.SH", 10)
    assert out["pre_close"] == 1330.75
    assert out["ticks"][0]["volume_lot"] == 3.0
    assert out["ticks"][0]["volume"] == 300       # 手 → 股
    assert out["ticks"][0]["amount"] == 406200.0
    assert out["ticks"][0]["direction"] == "sell"  # 东财 1 = 卖盘


def test_ticks_fall_back_to_tencent(monkeypatch):
    def em_boom(url, **kw):
        raise RuntimeError("eastmoney details blocked")

    info = 'v_detail_time_sh600519=[20261029,"09:25:01~09:33:24|09:33:27~09:36:57"]'
    data = ('v_detail_data_sh600519=[1,"70/09:33:27/1335.01/0.58/15/2001780/B|'
            '71/09:33:30/1335.40/0.39/5/667543/S"]')

    monkeypatch.setattr(mf.mp, "_get_json", em_boom)
    monkeypatch.setattr(mf.mp, "_get_text",
                        lambda url, **kw: info if "action=info" in url else data)
    monkeypatch.setattr(mf, "_pre_close", lambda code: 1330.75)

    out = mf.get_ticks("600519.SH", 5)
    assert out["source"].startswith("tencent")
    assert out["count"] == 2
    assert out["ticks"][0]["price"] == 1335.01
    assert out["ticks"][0]["volume"] == 1500      # 15 手
    assert out["ticks"][0]["direction"] == "buy"  # B
    assert out["ticks"][1]["direction"] == "sell"  # S
