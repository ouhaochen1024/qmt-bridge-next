"""qmt-bridge-next — 大 QMT 信箱桥接（外部端）。

本包不再 import xtquant，也不再直连 miniQMT。所有交易/查询请求经
文件信箱协议进入大 QMT 内置 Python（mailbox/qmt_mailbox_executor.py），
本包负责把信箱协议翻译成 HTTP/WS。

启动时如果检测到任何 xtquant 导入路径，属于构建错误：本构建只支持
mailbox executor。
"""

__version__ = "3.0.0"
