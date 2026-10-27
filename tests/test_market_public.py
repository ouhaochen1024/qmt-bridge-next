"""公开行情源解析单测（离线，不依赖网络）。

锁定两个回归点：
1. 腾讯五档字段方向（9~18 买盘 / 19~28 卖盘），历史上曾买卖互换；
2. `lastClose` 兼容别名（Skill 侧长期读这个字段名）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qmt_bridge import market_public as mp  # noqa: E402


def _fake_tencent_line() -> str:
    parts = [""] * 39
    parts[0] = "51"
    parts[1] = "平安银行"
    parts[2] = "000001"
    parts[3] = "11.47"      # 现价
    parts[4] = "11.30"      # 昨收
    parts[5] = "11.40"      # 今开
    parts[6] = "123456"     # 成交量（手）
    # 买一..买五（9~18）：价格递减
    for i, (price, vol) in enumerate([
        (11.46, 1500), (11.45, 1200), (11.44, 900),
        (11.43, 800), (11.42, 700),
    ]):
        parts[9 + i * 2] = str(price)
        parts[10 + i * 2] = str(vol)
    # 卖一..卖五（19~28）：价格递增
    for i, (price, vol) in enumerate([
        (11.47, 4787), (11.48, 757), (11.49, 1074),
        (11.50, 2949), (11.51, 2968),
    ]):
        parts[19 + i * 2] = str(price)
        parts[20 + i * 2] = str(vol)
    parts[33] = "11.60"
    parts[34] = "11.20"
    parts[37] = "123456.78"
    return 'v_sz000001="%s";' % "~".join(parts)


def test_tencent_orderbook_direction(monkeypatch):
    monkeypatch.setattr(mp, "_get_text", lambda url, **kw: _fake_tencent_line())
    tick = mp.get_full_tick(["000001.SZ"])["000001.SZ"]

    assert tick["lastPrice"] == 11.47
    # 卖一>=现价、买一<=现价，且各自按盘口顺序排列
    assert tick["askPrice"] == [11.47, 11.48, 11.49, 11.50, 11.51]
    assert tick["bidPrice"] == [11.46, 11.45, 11.44, 11.43, 11.42]
    assert tick["askVol"] == [4787, 757, 1074, 2949, 2968]
    assert tick["bidVol"] == [1500, 1200, 900, 800, 700]


def test_last_close_alias_present(monkeypatch):
    monkeypatch.setattr(mp, "_get_text", lambda url, **kw: _fake_tencent_line())
    tick = mp.get_full_tick(["000001.SZ"])["000001.SZ"]
    assert tick["lastClosed"] == 11.30
    # Skill 侧读 lastClose，缺这个别名会让涨跌幅恒为 0
    assert tick["lastClose"] == 11.30
