"""复用已有万行样例，经真实Tkinter入口再生成一遍并核对；不覆盖样例。"""
from __future__ import annotations

from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch


BASE = Path(__file__).resolve().parent
CONFIG = BASE / "1876_示例配置.json"
BASELINE = BASE / "1876_示例结果" if (BASE / "1876_示例结果").is_dir() else BASE
RECORD = BASE / "1876_复现核对记录.json"
COUNTERS = ("rows", "unique_ids", "sqlite_rows", "verified_matches")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sql_records(path):
    """事务上下文不等于关闭连接；Windows清理前必须显式close。"""
    with closing(sqlite3.connect(path)) as connection:
        with closing(connection.execute("SELECT * FROM records ORDER BY record_index")) as cursor:
            names = [item[0] for item in cursor.description]
            return names, cursor.fetchall()


def csv_records(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        return next(reader), list(reader)


def logical_hash(rows):
    result = hashlib.sha256()
    for row in rows:
        text = json.dumps([str(value) for value in row], ensure_ascii=False, separators=(",", ":")) + "\n"
        result.update(text.encode("utf-8"))
    return result.hexdigest()


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def main():
    require(all((BASELINE / filename).is_file() for filename in (
        "1876_测试数据.csv", "1876_测试数据.sqlite", "1876_审计记录.json", "1876_运行报告.html")),
        "需要已有1876_示例结果或仓库根目录平铺的四个样例文件；本脚本不创建或覆盖基准样例")
    require(CONFIG.is_file(), "缺少示例配置")
    config_hash = digest(CONFIG)
    baseline_audit = json.loads((BASELINE / "1876_审计记录.json").read_text(encoding="utf-8"))
    require(baseline_audit["source_config_sha256"] == config_hash, "基准样例使用了不同的配置源")
    require(all(baseline_audit[key] == 10000 for key in COUNTERS), "基准样例的四项计数必须全部为10000")
    baseline_paths = {key: BASELINE / value for key, value in baseline_audit["outputs"].items()}
    baseline_hashes = {path.name: digest(path) for path in baseline_paths.values()}
    baseline_fields, baseline_sql = sql_records(baseline_paths["sqlite"])
    csv_fields, baseline_csv = csv_records(baseline_paths["csv"])
    baseline_logical = logical_hash(baseline_sql)
    require(baseline_fields == csv_fields, "基准样例CSV与SQL字段顺序不同")
    require(len(baseline_csv) == len(baseline_sql) == 10000, "基准样例实际行数不为10000")
    require(baseline_csv == [[str(value) for value in row] for row in baseline_sql], "基准样例CSV与SQL记录不一致")
    require(baseline_logical == baseline_audit["ordered_logical_sha256"], "基准业务哈希不能独立复算")
    spec = importlib.util.spec_from_file_location("reproduce_gui_1876", BASE / "1876_桌面工具.py")
    gui = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gui
    spec.loader.exec_module(gui)
    print("Baseline checked: 10,000 CSV rows and 10,000 ordered SQLite rows.", flush=True)
    record = None
    with tempfile.TemporaryDirectory(prefix="1876_实际运行_") as temporary:
        temporary_root = Path(temporary)
        actual_output = temporary_root / "1876_第二次万行输出"
        edited_output = temporary_root / "1876_运行中编辑的输出位置"
        entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
        real_generate = gui.engine.generate
        errors = []

        def controlled_generate(*args):
            entered.set()
            try:
                require(resume.wait(10), "GUI任务未被恢复")
                return real_generate(*args)
            finally:
                finished.set()

        app = gui.App()
        app.withdraw()
        try:
            with patch.object(gui.engine, "generate", controlled_generate), \
                 patch.object(gui.messagebox, "showerror", side_effect=lambda *args, **kwargs: errors.append(args)), \
                 patch.object(gui.webbrowser, "open", return_value=True) as open_browser:
                app.config_path.set(str(CONFIG))
                app.output_path.set(str(actual_output))
                app.start()
                require(entered.wait(5), "GUI后台任务没有启动")
                require(app.active_output == actual_output, "GUI没有保存启动时的输出路径")
                app.output_path.set(str(edited_output))
                require(app.output_path.get() == str(edited_output), "未成功模拟运行中编辑输出位置")
                resume.set()
                deadline = time.monotonic() + 180
                while app.result is None and not errors:
                    require(time.monotonic() < deadline, "GUI生成等待超过180秒")
                    app.update()
                    time.sleep(0.01)
                require(not errors, f"GUI生成报错：{errors}")
                require(finished.wait(5), "后台生成线程未完成")
                result = app.result
                require(all(result[key] == 10000 for key in COUNTERS), "第二遍生成四项计数必须全部为10000")
                displayed = {key: variable.get() for key, variable in app.values.items()}
                require(all(displayed[key] == "10,000" for key in COUNTERS), "GUI四个计数未全部显示10,000")
                actual_paths = {key: actual_output / value for key, value in result["outputs"].items()}
                actual_fields, actual_sql = sql_records(actual_paths["sqlite"])
                require(baseline_fields == actual_fields, "两次SQL字段顺序不同")
                require(baseline_sql == actual_sql, "两次有序SQL业务行不同")
                require(baseline_paths["csv"].read_bytes() == actual_paths["csv"].read_bytes(), "两次CSV业务字节不同")
                actual_logical = logical_hash(actual_sql)
                require(actual_logical == baseline_logical == result["ordered_logical_sha256"], "两次业务哈希不同")
                expected_report = actual_output / "1876_运行报告.html"
                require(app.report == expected_report, "运行中改动路径导致报告指向错误位置")
                require(str(app.open_button.cget("state")) == "normal", "报告按钮没有启用")
                app.open_button.invoke()
                open_browser.assert_called_once_with(expected_report.resolve().as_uri())
                require(not edited_output.exists(), "GUI错误地使用了运行中编辑的输出位置")
                require(digest(CONFIG) == config_hash, "实际运行修改了配置源")
                require({path.name: digest(path) for path in baseline_paths.values()} == baseline_hashes, "已有基准样例被修改")
                record = {
                    "schema_version": 1, "status": "passed", "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "config": CONFIG.name, "baseline_directory": BASELINE.name,
                    "counts": {key: result[key] for key in COUNTERS}, "displayed_counters": displayed,
                    "checks": {"csv_bytes_equal": True, "ordered_sql_rows_equal": True, "csv_sql_values_equal": True,
                               "independent_logical_hash_equal": True, "source_config_unchanged": True,
                               "baseline_outputs_unchanged": True, "output_edit_during_generation_verified": True,
                               "report_button_uri_matches_actual_output": True, "edited_output_not_created": True},
                    "source_config_sha256": config_hash, "csv_sha256": digest(actual_paths["csv"]),
                    "ordered_logical_sha256": actual_logical,
                    "report_button": {"scheme": "file", "filename": expected_report.name, "opens_start_time_output": True},
                    "sqlite_connection_policy": "contextlib.closing closes every connection and cursor before temporary cleanup",
                    "comparison_policy": "Compare CSV bytes, ordered SQL rows and logical hashes; do not require equal SQLite file hashes.",
                }
                print("GUI checked: all four counters are 10,000; edited path does not redirect its report.", flush=True)
        finally:
            resume.set()
            if entered.is_set():
                finished.wait(180)
            app.destroy()
    # Reaching here proves Windows could remove the temporary SQLite outputs.
    record["checks"]["temporary_directory_cleanup_succeeded"] = True
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="1876_核对暂存_", suffix=".json", dir=BASE, delete=False) as stream:
        temporary_record = Path(stream.name)
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary_record.replace(RECORD)
    print("Passed. Verification record saved: " + RECORD.name, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
