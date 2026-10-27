#!/usr/bin/env python3
"""stamp_core.py — 统一配置盖戳核心（由 stamp_executor.sh 调用）。

读取 <repo>/config/config.ini（唯一配置源），把两个常量幂等写进
mailbox/qmt_mailbox_executor.py：
    BASE_DIR   = <base_dir>          信箱目录
    CONFIG_INI = <config.ini 绝对路径>  统一配置

纯 Python 字符串操作（无正则替换、无 shell 转义），Windows 路径反斜杠
自动双写为 Python 字面量转义，并回读校验。

用法:
    python stamp_core.py --mailbox <mailbox目录> [--check]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def read_config(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip().lower()] = value.strip().strip('"').strip("'")
    return cfg


def py_literal(value: str) -> str:
    """转成可安全内嵌的 Python 字符串字面量（反斜杠双写）。"""
    return '"%s"' % value.replace("\\", "\\\\")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    mailbox = Path(args.mailbox).resolve()
    config_path = REPO_ROOT / "config" / "config.ini"
    if not config_path.is_file():
        print(f"错误: 找不到统一配置 {config_path}", file=sys.stderr)
        return 2
    cfg = read_config(config_path)
    base_dir = cfg.get("base_dir", "").strip()
    account_id = cfg.get("account_id", "").strip()
    if not base_dir:
        print("错误: config.ini 缺少 base_dir", file=sys.stderr)
        return 2
    base = Path(base_dir)
    if not base.is_dir():
        print(f"错误: base_dir 不存在: {base}", file=sys.stderr)
        return 2
    config_abs = str(config_path.resolve())

    executor = base / "qmt_mailbox_executor.py"
    if not executor.is_file():
        executor = mailbox / "qmt_mailbox_executor.py"
    if not executor.is_file():
        print("错误: 找不到 qmt_mailbox_executor.py", file=sys.stderr)
        return 2

    text = executor.read_text(encoding="utf-8")
    lines = text.splitlines()

    want = {
        "BASE_DIR": py_literal(str(base)),
        "CONFIG_INI": py_literal(config_abs),
    }

    # 现状校验
    current: dict[str, str] = {}
    others: list[str] = []
    for line in lines:
        stripped = line.strip()
        matched = False
        for name in want:
            if stripped.startswith(name + " = ") or stripped == name + ' = ""':
                current[name] = stripped
                matched = True
                break
        if not matched:
            others.append(line)

    def ok() -> bool:
        try:
            for name, literal in current.items():
                if ast.literal_eval(literal.split("=", 1)[1].strip()) != \
                        ast.literal_eval(want[name]):
                    return False
            return set(current) == set(want)
        except (ValueError, SyntaxError):
            return False

    if ok():
        print(f"[check] 盖戳已是最新（BASE_DIR/CONFIG_INI → {base}）")
        return 0
    if args.check:
        print(f"[check] 尚未盖戳或已过期（将写入 BASE_DIR/CONFIG_INI → {base}）")
        return 0

    # 重建: 以 want 里的两行替换/插入（保持行内位置在原 BASE_DIR 处或
    # "# Filled by" 注释之前）
    out: list[str] = []
    inserted = False
    for line in others:
        if not inserted and line.strip().startswith("# Filled by"):
            out.append("BASE_DIR = %s" % want["BASE_DIR"])
            out.append("CONFIG_INI = %s" % want["CONFIG_INI"])
            inserted = True
        out.append(line)
    if not inserted:
        out.extend(["BASE_DIR = %s" % want["BASE_DIR"],
                    "CONFIG_INI = %s" % want["CONFIG_INI"]])

    executor.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[stamp] 已写入: {executor}")

    # 回读校验
    new_text = executor.read_text(encoding="utf-8")
    new_lines = [ln for ln in new_text.splitlines()
                 if ln.strip().startswith(("BASE_DIR = ", "CONFIG_INI = "))]
    for ln in new_lines:
        name, _, literal = ln.strip().partition(" = ")
        value = ast.literal_eval(literal.strip())
        if value != ast.literal_eval(want[name.strip()]):
            print(f"错误: {name} 字面量解析为 {value!r}（转义损坏）",
                  file=sys.stderr)
            return 2
        print(f"[verify] {name.strip()} = {value}")
    print(f"[ok] account_id = {account_id or '（未配置）'}")
    print(f"[ok] 信箱目录   = {base}")
    print(f"[next] 把 {executor} 全文粘贴进 QMT 策略并重启。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
