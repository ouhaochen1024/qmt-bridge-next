"""桥接端配置 ── 唯一配置源: config\config.ini。

桥接 / 大 QMT 执行器 / Agent Skills 三端都只读 config\config.ini，
不再使用 .env。支持键（井号/分号后为注释，值可带引号）：

    host / port            桥接 HTTP 监听
    api_key                API Key（交易写操作必需）
    account_id             资金账号（执行器启动时 set_account）
    base_dir               信箱目录（执行器 BASE_DIR 盖戳目标）

查找顺序（全部绝对定位，与启动 cwd 无关）：
    1. $QMT_CONFIG 环境变量指定的 config.ini
    2. <桥接仓库>\config\config.ini（由本文件位置锚定）
    3. cwd\config\config.ini（兜底）

解析约定：config.ini 的键先映射为 os.environ 的 QMT_BRIDGE_* 变量
（仅当外部环境未显式设置时，运维仍可用环境变量临时覆盖），再构造
Settings。信箱目录优先级：显式 QMT_BRIDGE_MAILBOX > base_dir。

设计约束（对应执行器 qmt_mailbox_executor.py）：
- 桥接端不决定账户 id：account_id 来自 config.ini（运维显式声明），
  请求里的 account_id 为空时由执行器使用其绑定的账户。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 桥接仓库根（由本文件位置锚定: <repo>/qmt_bridge/config.py）
_REPO_ROOT = Path(__file__).resolve().parents[1]

# config.ini 键 → 环境变量映射
_INI_TO_ENV = {
    "host": "QMT_BRIDGE_HOST",
    "port": "QMT_BRIDGE_PORT",
    "api_key": "QMT_BRIDGE_API_KEY",
    "account_id": "QMT_BRIDGE_STOCK_ACCOUNT_ID",
}

# 允许相对路径的键（相对 config.ini 所在目录解析）
_PATH_KEYS = {"base_dir"}


def _env_bool(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _find_config_ini() -> Path | None:
    candidates = []
    qmt_config = os.environ.get("QMT_CONFIG", "").strip()
    if qmt_config:
        candidates.append(Path(qmt_config))
    candidates.append(_REPO_ROOT / "config" / "config.ini")
    candidates.append(Path.cwd() / "config" / "config.ini")
    for p in candidates:
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def _load_config_ini(path: Path) -> dict[str, str]:
    """解析 config.ini 为 {key: value}（井号/分号注释，值可带引号）。"""
    cfg: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key:
            cfg[key] = value
    return cfg


def _apply_config_ini() -> dict[str, str]:
    """加载 config.ini: 键映射进 os.environ（不覆盖显式环境变量）。

    返回解析出的完整键值对（供 base_dir 等路径键直接使用）。
    """
    path = _find_config_ini()
    if path is None:
        return {}
    cfg = _load_config_ini(path)
    for key, value in cfg.items():
        env_name = _INI_TO_ENV.get(key)
        if env_name and env_name not in os.environ:
            os.environ[env_name] = value
    # 路径键: 相对路径按 config.ini 所在目录解析
    for key in _PATH_KEYS:
        raw = cfg.get(key, "")
        if raw and not Path(raw).is_absolute():
            cfg[key] = str((path.parent / raw).resolve())
    return cfg


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    api_key: str = ""
    require_auth_for_data: bool = False

    # 信箱目录（config.ini base_dir；空则回落 <repo>/mailbox）
    mailbox: str = ""

    # 写门禁：False 时所有下单/撤单路由直接拒绝。
    order_writes_enabled: bool = True

    # 执行器存活判定
    hb_max_age_sec: float = 5.0

    # 信箱请求
    request_timeout_sec: float = 15.0
    poll_interval_sec: float = 0.05

    # 事件队列上限（WS 推送缓冲）
    event_queue_max: int = 1000

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "Settings":
        cfg = _apply_config_ini()
        settings = cls(
            host=os.environ.get("QMT_BRIDGE_HOST", "0.0.0.0"),
            port=int(os.environ.get("QMT_BRIDGE_PORT", "8000")),
            log_level=os.environ.get("QMT_BRIDGE_LOG_LEVEL", "info"),
            api_key=os.environ.get("QMT_BRIDGE_API_KEY", ""),
            require_auth_for_data=_env_bool("QMT_BRIDGE_REQUIRE_AUTH_FOR_DATA"),
            # 信箱路径优先级: 显式 QMT_BRIDGE_MAILBOX（运行期覆盖）>
            # config.ini base_dir > <repo>/mailbox 回退。
            mailbox=os.environ.get("QMT_BRIDGE_MAILBOX", "")
            or cfg.get("base_dir", ""),
            order_writes_enabled=not _env_bool(
                "QMT_BRIDGE_ORDER_WRITES_DISABLED"
            )
            and _env_bool("QMT_BRIDGE_ORDER_WRITES_ENABLED", "true"),
            hb_max_age_sec=float(
                os.environ.get("QMT_BRIDGE_EXECUTOR_HB_MAX_AGE_SEC", "5")
            ),
            request_timeout_sec=float(
                os.environ.get("QMT_BRIDGE_INTERNAL_REQUEST_TIMEOUT_SEC", "15")
            ),
            poll_interval_sec=float(
                os.environ.get("QMT_BRIDGE_MAILBOX_POLL_INTERVAL_SEC", "0.05")
            ),
        )
        return settings

    def resolved_mailbox(self) -> Path:
        raw = (self.mailbox or "").strip()
        if raw:
            return Path(raw)
        return _REPO_ROOT / "mailbox"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings(settings: Settings | None = None) -> None:
    global _settings
    _settings = settings
