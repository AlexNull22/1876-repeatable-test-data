"""可重复测试数据生成器的业务验收；仅使用标准库和临时目录。"""
from __future__ import annotations

import copy
import csv
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from uuid import UUID


ENGINE_PATH = Path(__file__).with_name("1876_数据生成.py")
SPEC = importlib.util.spec_from_file_location("test_data_generator_1876", ENGINE_PATH)
engine = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = engine
SPEC.loader.exec_module(engine)

BASE_CONFIG = {
    "version": 1, "dataset_id": "demo-orders", "seed": 20261019, "rows": 128,
    "fields": [
        {"name": "order_number", "type": "sequence", "start": 100, "step": 3},
        {"name": "region", "type": "choice", "values": ["华东", "华南", "华北"]},
        {"name": "quantity", "type": "integer", "min": 1, "max": 5},
        {"name": "amount", "type": "decimal", "min": "5.00", "max": "499.99", "places": 2},
        {"name": "order_date", "type": "date", "min": "2026-01-01", "max": "2026-10-19"},
        {"name": "is_active", "type": "boolean", "true_probability": "0.8"},
    ],
}
FIELD_NAMES = ["id", "record_index", "order_number", "region", "quantity", "amount", "order_date", "is_active"]


class RepeatableDataAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="1876_验收_")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def config(self):
        return copy.deepcopy(BASE_CONFIG)

    def write_config(self, config, tag="默认", compact=False):
        path = self.root / f"1876_配置_{tag}.json"
        if compact:
            text = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            text = json.dumps(config, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def generate(self, config=None, tag="默认", compact=False):
        config = self.config() if config is None else config
        source = self.write_config(config, tag, compact)
        output = self.root / f"1876_中文输出_{tag}"
        original_hash = self.digest(source)
        audit = engine.generate(source, output)
        self.assertEqual(audit["status"], "complete")
        self.assertEqual(audit["rows"], config["rows"])
        self.assertEqual(set(audit["outputs"]), {"csv", "sqlite", "json", "html"})
        paths = {key: output / value for key, value in audit["outputs"].items()}
        for path in paths.values():
            self.assertEqual(path.parent, output)
            self.assertTrue(path.name.startswith("1876_"))
            self.assertTrue(path.is_file())
        self.assertEqual(self.digest(source), original_hash, "配置源文件发生改变")
        return audit, paths, source

    @staticmethod
    def csv_rows(path):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)

    @staticmethod
    def sql_rows(path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM records ORDER BY record_index")]
        finally:
            connection.close()

    def assert_invalid(self, config, tag):
        before = copy.deepcopy(config)
        with self.assertRaises(engine.WorkflowError) as caught:
            engine.validate_config(config)
        self.assertTrue(str(caught.exception))
        self.assertEqual(config, before, "验证失败时修改了调用者的配置")
        source = self.write_config(config, tag)
        output = self.root / f"1876_拒绝输出_{tag}"
        source_hash = self.digest(source)
        with self.assertRaises(engine.WorkflowError):
            engine.generate(source, output)
        self.assertFalse(output.exists() and any(output.iterdir()), "失败时留下部分输出")
        self.assertEqual(source_hash, self.digest(source))

    def test_validation_returns_new_config_without_mutating_input(self):
        config = self.config()
        before = copy.deepcopy(config)
        validated = engine.validate_config(config)
        self.assertEqual(validated, before)
        self.assertIsNot(validated, config)
        self.assertEqual(config, before)

    def test_same_config_repeats_business_csv_bytes_and_ordered_sql_rows(self):
        _, first, _ = self.generate(tag="第一批")
        _, second, _ = self.generate(tag="第二批")
        self.assertEqual(first["csv"].read_bytes(), second["csv"].read_bytes())
        self.assertEqual(self.sql_rows(first["sqlite"]), self.sql_rows(second["sqlite"]))

    def test_uuid5_ids_are_unique_and_stable_for_existing_record_indices(self):
        config = self.config()
        config["rows"] = 1000
        audit, paths, _ = self.generate(config, "一千行")
        rows = self.sql_rows(paths["sqlite"])
        ids = [row["id"] for row in rows]
        self.assertEqual(len(set(ids)), 1000)
        self.assertEqual([row["record_index"] for row in rows], list(range(1, 1001)))
        self.assertTrue(all(UUID(value).version == 5 for value in ids))
        self.assertEqual(audit["unique_ids"], 1000)
        shorter = self.config()
        shorter["rows"] = 10
        _, small_paths, _ = self.generate(shorter, "十行")
        self.assertEqual(ids[:10], [row["id"] for row in self.sql_rows(small_paths["sqlite"])])

    def test_seed_and_dataset_id_isolate_record_identity(self):
        original = self.config()
        original["rows"] = 64
        seed_changed, dataset_changed = copy.deepcopy(original), copy.deepcopy(original)
        seed_changed["seed"] += 1
        dataset_changed["dataset_id"] = "independent-orders"
        groups = []
        for tag, config in (("原始", original), ("新种子", seed_changed), ("新数据集", dataset_changed)):
            _, paths, _ = self.generate(config, tag)
            groups.append({row["id"] for row in self.sql_rows(paths["sqlite"])})
        self.assertFalse(groups[0] & groups[1])
        self.assertFalse(groups[0] & groups[2])
        self.assertFalse(groups[1] & groups[2])

    def test_all_six_types_respect_bounds_precision_and_sql_storage_types(self):
        _, paths, _ = self.generate()
        rows = self.sql_rows(paths["sqlite"])
        for index, row in enumerate(rows, 1):
            self.assertEqual(row["order_number"], 100 + (index - 1) * 3)
            self.assertIn(row["region"], {"华东", "华南", "华北"})
            self.assertIsInstance(row["quantity"], int)
            self.assertLessEqual(1, row["quantity"])
            self.assertLessEqual(row["quantity"], 5)
            self.assertIsInstance(row["amount"], str)
            self.assertRegex(row["amount"], r"^\d+\.\d{2}$")
            self.assertLessEqual(Decimal("5.00"), Decimal(row["amount"]))
            self.assertLessEqual(Decimal(row["amount"]), Decimal("499.99"))
            self.assertLessEqual(date(2026, 1, 1), date.fromisoformat(row["order_date"]))
            self.assertLessEqual(date.fromisoformat(row["order_date"]), date(2026, 10, 19))
            self.assertIn(row["is_active"], (0, 1))
        connection = sqlite3.connect(paths["sqlite"])
        try:
            stored = connection.execute("SELECT typeof(order_number),typeof(quantity),typeof(amount),typeof(order_date),typeof(region),typeof(is_active) FROM records LIMIT 1").fetchone()
            self.assertEqual(stored, ("integer", "integer", "text", "text", "text", "integer"))
        finally:
            connection.close()

    def test_csv_and_sqlite_values_and_independent_logical_hash_match(self):
        audit, paths, _ = self.generate()
        fields, csv_records = self.csv_rows(paths["csv"])
        sql_records = self.sql_rows(paths["sqlite"])
        self.assertEqual(fields, FIELD_NAMES)
        self.assertTrue(paths["csv"].read_bytes().startswith(b"\xef\xbb\xbf"))
        canonical = hashlib.sha256()
        for csv_row, sql_row in zip(csv_records, sql_records):
            strings = {name: str(sql_row[name]) for name in FIELD_NAMES}
            self.assertEqual(csv_row, strings)
            canonical.update((json.dumps([strings[name] for name in FIELD_NAMES], ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        self.assertEqual(len(csv_records), 128)
        self.assertEqual(audit["sqlite_rows"], 128)
        self.assertEqual(audit["verified_matches"], 128)
        self.assertEqual(audit["ordered_logical_sha256"], canonical.hexdigest())

    def test_boolean_probability_zero_and_one_are_exact(self):
        for probability, expected in (("0", 0), ("1", 1)):
            with self.subTest(probability=probability):
                config = self.config()
                config["rows"] = 16
                config["fields"][5]["true_probability"] = probability
                _, paths, _ = self.generate(config, "概率" + probability)
                self.assertEqual({row["is_active"] for row in self.sql_rows(paths["sqlite"])}, {expected})

    def test_single_value_ranges_and_descending_sequences_are_exact(self):
        config = self.config()
        config["rows"] = 5
        config["fields"][0].update(start=4, step=-2)
        config["fields"][1]["values"] = ["唯一值"]
        config["fields"][2].update(min=7, max=7)
        config["fields"][3].update(min="12.30", max="12.30")
        config["fields"][4].update(min="2026-10-19", max="2026-10-19")
        _, paths, _ = self.generate(config)
        rows = self.sql_rows(paths["sqlite"])
        self.assertEqual([row["order_number"] for row in rows], [4, 2, 0, -2, -4])
        for row in rows:
            self.assertEqual((row["region"], row["quantity"], row["amount"], row["order_date"]), ("唯一值", 7, "12.30", "2026-10-19"))

    def test_equivalent_json_formatting_has_same_canonical_config_hash(self):
        left, left_paths, left_source = self.generate(tag="缩进")
        right, right_paths, right_source = self.generate(tag="紧凑", compact=True)
        self.assertNotEqual(left_source.read_bytes(), right_source.read_bytes())
        self.assertEqual(left["canonical_config_sha256"], right["canonical_config_sha256"])
        self.assertNotEqual(left["source_config_sha256"], right["source_config_sha256"])
        self.assertEqual(left_paths["csv"].read_bytes(), right_paths["csv"].read_bytes())

    def test_formula_like_choices_are_safe_in_csv_and_literal_in_sqlite(self):
        payloads = ["=1+1", "+SUM(1,2)", "-9+2", "@cmd", "'already quoted", '<script>alert(1876)</script>', "中文内容"]
        for index, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                config = self.config()
                config["rows"] = 1
                config["fields"][1]["values"] = [payload]
                _, paths, _ = self.generate(config, f"文本{index}")
                _, csv_records = self.csv_rows(paths["csv"])
                self.assertEqual(self.sql_rows(paths["sqlite"])[0]["region"], payload)
                expected = "'" + payload if payload.startswith(("=", "+", "-", "@")) else payload
                self.assertEqual(csv_records[0]["region"], expected)
                if payload.startswith("<script>"):
                    document = paths["html"].read_text(encoding="utf-8")
                    self.assertNotIn(payload, document)
                    self.assertIn("&lt;script&gt;alert(1876)&lt;/script&gt;", document)

    def test_unknown_missing_and_wrong_root_or_field_keys_are_rejected(self):
        cases = []
        config = self.config(); config["unexpected"] = True; cases.append(config)
        config = self.config(); del config["seed"]; cases.append(config)
        config = self.config(); config["version"] = 2; cases.append(config)
        config = self.config(); config["fields"] = []; cases.append(config)
        config = self.config(); config["fields"][0]["unexpected"] = "x"; cases.append(config)
        config = self.config(); del config["fields"][0]["step"]; cases.append(config)
        config = self.config(); config["fields"][0]["type"] = "arbitrary_code"; cases.append(config)
        for index, case in enumerate(cases):
            with self.subTest(index=index):
                self.assert_invalid(case, f"配置键{index}")

    def test_rows_and_seed_require_bounded_integers_excluding_booleans(self):
        for key, values in (("rows", [0, -1, 100001, True, 1.5, "10"]),
                            ("seed", [True, 1.5, "10", 2 ** 63, -(2 ** 63) - 1])):
            for index, value in enumerate(values):
                with self.subTest(key=key, value=value):
                    config = self.config()
                    config[key] = value
                    self.assert_invalid(config, f"{key}边界{index}")

    def test_nonfinite_bool_integer_scale_and_sequence_overflow_are_rejected(self):
        cases = [(0, "start", True), (0, "step", False), (0, "step", 0), (2, "min", True),
                 (3, "min", "NaN"), (3, "max", "Infinity"), (3, "min", 5),
                 (3, "places", True), (3, "places", 9), (3, "min", "5.001"),
                 (5, "true_probability", "NaN"), (5, "true_probability", "Infinity"), (5, "true_probability", 0.8)]
        for index, (field, key, value) in enumerate(cases):
            with self.subTest(field=field, key=key, value=value):
                config = self.config()
                config["fields"][field][key] = value
                self.assert_invalid(config, f"数值类型{index}")
        for index, (start, step) in enumerate(((2 ** 63 - 1, 1), (-(2 ** 63), -1))):
            config = self.config()
            config["rows"] = 2
            config["fields"][0].update(start=start, step=step)
            self.assert_invalid(config, f"序列溢出{index}")

    def test_sql_field_names_are_safe_and_unique_ignoring_case(self):
        names = ["id", "ID", "record_index", "sqlite_shadow", "select", "from", "a;DROP TABLE records", "a b", "1start", "中文列", "a" * 64]
        for index, name in enumerate(names):
            with self.subTest(name=name):
                config = self.config()
                config["fields"][0]["name"] = name
                self.assert_invalid(config, f"字段名{index}")
        config = self.config()
        config["fields"][0]["name"] = "OrderNumber"
        config["fields"][1]["name"] = "ordernumber"
        self.assert_invalid(config, "字段忽略大小写重复")

    def test_choice_date_numeric_bounds_and_probability_rules_are_strict(self):
        cases = [(1, {"values": []}), (1, {"values": ["ok", 12]}), (1, {"values": [""]}),
                 (1, {"values": ["line\nbreak"]}), (1, {"values": ["nul\x00byte"]}),
                 (2, {"min": 6, "max": 5}), (3, {"min": "500.00", "max": "5.00"}),
                 (4, {"min": "2026-02-30"}), (4, {"min": "01/02/2026"}),
                 (4, {"min": "2026-10-20", "max": "2026-10-19"}),
                 (5, {"true_probability": "-0.01"}), (5, {"true_probability": "1.01"})]
        for index, (field, changes) in enumerate(cases):
            with self.subTest(field=field, changes=changes):
                config = self.config()
                config["fields"][field].update(changes)
                self.assert_invalid(config, f"业务约束{index}")

    def test_existing_output_is_preserved_and_source_config_is_unchanged(self):
        source = self.write_config(self.config())
        source_hash = self.digest(source)
        output = self.root / "1876_已有输出"
        output.mkdir()
        sentinel = output / "1876_原有文件.txt"
        sentinel.write_text("keep unchanged", encoding="utf-8")
        with self.assertRaises(engine.WorkflowError):
            engine.generate(source, output)
        self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep unchanged")
        self.assertEqual(self.digest(source), source_hash)

    def test_cli_bad_config_fails_without_partial_output(self):
        source = self.root / "1876_损坏配置.json"
        source.write_text("{not valid json", encoding="utf-8")
        source_hash = self.digest(source)
        output = self.root / "1876_命令行拒绝输出"
        completed = subprocess.run([sys.executable, "-B", str(ENGINE_PATH), "--config", str(source), "--output", str(output)], capture_output=True, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout or completed.stderr)
        self.assertFalse(output.exists() and any(output.iterdir()))
        self.assertEqual(self.digest(source), source_hash)

    def test_unicode_directories_and_audit_file_hashes_are_complete(self):
        audit, paths, source = self.generate(tag="中文目录与空格 空间")
        self.assertEqual(audit["source_config_sha256"], self.digest(source))
        self.assertEqual(audit["csv_sha256"], self.digest(paths["csv"]))
        self.assertEqual(audit["sqlite_sha256"], self.digest(paths["sqlite"]))
        saved = json.loads(paths["json"].read_text(encoding="utf-8-sig"))
        self.assertEqual(saved["ordered_logical_sha256"], audit["ordered_logical_sha256"])
        self.assertEqual(saved["outputs"], audit["outputs"])
        self.assertLessEqual(len(audit["preview"]), 12)
        for filename in audit["outputs"].values():
            self.assertEqual(Path(filename).name, filename)
        self.assertNotIn(str(self.root), paths["json"].read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
