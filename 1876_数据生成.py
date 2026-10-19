"""Reproducible synthetic CSV and SQLite data. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

OUTPUTS = {"csv": "1876_测试数据.csv", "sqlite": "1876_测试数据.sqlite", "json": "1876_审计记录.json", "html": "1876_运行报告.html"}
INT_MIN, INT_MAX = -(2 ** 63), 2 ** 63 - 1
KEYWORDS = set("ABORT ACTION ADD AFTER ALL ALTER ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM FULL GENERATED GLOB GROUP GROUPS HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT INTO IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT SET TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT".split())
RULES = {"sequence": {"start", "step"}, "integer": {"min", "max"}, "decimal": {"min", "max", "places"}, "date": {"min", "max"}, "choice": {"values"}, "boolean": {"true_probability"}}
ID_RULE = {"algorithm": "UUIDv5", "namespace": str(uuid.NAMESPACE_URL), "identity": "Compact UTF-8 JSON array [1876-id-v1, dataset_id, seed, 1-based record_index]", "scope": "Same dataset_id, seed, and record_index yield the same ID. Field order and row count do not affect IDs.", "purpose": "Synthetic database identifiers; not security tokens."}


class WorkflowError(Exception):
    def __init__(self, message, issues=None):
        self.issues = list(issues or [])
        super().__init__(message + ("\n" + "\n".join("- " + item for item in self.issues) if self.issues else ""))


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def int64(value, label):
    if type(value) is not int or not INT_MIN <= value <= INT_MAX:
        raise ValueError(label + " must be a signed 64-bit integer (booleans are not integers)")
    return value


def decimal_string(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(label + " must be a decimal string")
    try:
        result = Decimal(value)
        if not result.is_finite() or len(result.as_tuple().digits) > 40 or abs(result.as_tuple().exponent) > 40 or abs(result.adjusted()) > 40:
            raise ValueError(label + " must be finite with at most 40 digits and exponent magnitude 40")
        return result
    except InvalidOperation as exc:
        raise ValueError(label + " is not a decimal number") from exc


def iso(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date bounds must use YYYY-MM-DD")
    return date.fromisoformat(value)


def validate_config(config):
    """Return a validated independent config copy without changing caller data."""
    try:
        if not isinstance(config, dict) or set(config) != {"version", "dataset_id", "seed", "rows", "fields"}:
            raise ValueError("top-level keys must be exactly version,dataset_id,seed,rows,fields")
        if type(config["version"]) is not int or config["version"] != 1:
            raise ValueError("version must be integer 1")
        identity = config["dataset_id"]
        if not isinstance(identity, str) or not identity or identity != identity.strip() or len(identity) > 128 or re.search(r"[\x00-\x1f\x7f]", identity):
            raise ValueError("dataset_id must be a trimmed, non-empty string of at most 128 characters without controls")
        identity.encode("utf-8")
        int64(config["seed"], "seed")
        if type(config["rows"]) is not int or not 1 <= config["rows"] <= 100000:
            raise ValueError("rows must be an integer from 1 to 100000")
        if not isinstance(config["fields"], list) or not 1 <= len(config["fields"]) <= 20:
            raise ValueError("fields must contain 1 to 20 field objects")
        names = set()
        with localcontext() as context:
            context.prec = 120
            for field in config["fields"]:
                if not isinstance(field, dict) or not isinstance(field.get("type"), str) or field["type"] not in RULES:
                    raise ValueError("each field requires a supported type: " + ", ".join(RULES))
                kind, name = field["type"], field.get("name")
                if set(field) != {"name", "type"} | RULES[kind]:
                    raise ValueError(f"{name}: keys must be exactly name,type," + ",".join(sorted(RULES[kind])))
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
                    raise ValueError("field names must be ASCII identifiers of 1 to 63 characters")
                if name.casefold() in names or name.casefold() in {"id", "record_index", "rowid", "_rowid_", "oid"} or name.lower().startswith("sqlite_") or name.upper() in KEYWORDS:
                    raise ValueError(name + ": duplicate, reserved, or conflicting SQLite field name")
                names.add(name.casefold())
                if kind == "sequence":
                    start, step = int64(field["start"], name + ".start"), int64(field["step"], name + ".step")
                    if step == 0:
                        raise ValueError(name + ": sequence step cannot be zero")
                    int64(start + step * (config["rows"] - 1), name + ".last_value")
                elif kind == "integer":
                    if int64(field["min"], name + ".min") > int64(field["max"], name + ".max"):
                        raise ValueError(name + ": min exceeds max")
                elif kind == "decimal":
                    places = field["places"]
                    if type(places) is not int or not 0 <= places <= 8:
                        raise ValueError(name + ": places must be an integer from 0 to 8")
                    low, high = decimal_string(field["min"], name + ".min"), decimal_string(field["max"], name + ".max")
                    unit = Decimal(1).scaleb(-places)
                    if low > high or low != low.quantize(unit) or high != high.quantize(unit):
                        raise ValueError(name + ": bounds are reversed or not exactly representable at places")
                elif kind == "date":
                    if iso(field["min"]) > iso(field["max"]):
                        raise ValueError(name + ": date min exceeds max")
                elif kind == "choice":
                    values = field["values"]
                    if not isinstance(values, list) or not 1 <= len(values) <= 1000 or any(not isinstance(value, str) or not value or len(value) > 1000 or re.search(r"[\x00-\x1f\x7f]", value) for value in values):
                        raise ValueError(name + ": values must contain 1..1000 non-empty strings, each <=1000 characters without controls")
                    for value in values:
                        value.encode("utf-8")
                elif kind == "boolean":
                    probability = decimal_string(field["true_probability"], name + ".true_probability")
                    if not 0 <= probability <= 1:
                        raise ValueError(name + ": true_probability must be between 0 and 1")
        return json.loads(json.dumps(config, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        raise WorkflowError("Invalid generator configuration: " + str(exc)) from exc


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def draw(seed, low, high):
    """SHA256 counter rejection sampling; independent of Python's hash/PRNG state."""
    span, counter = high - low + 1, 0
    limit = (1 << 256) - (1 << 256) % span
    while True:
        value = int.from_bytes(hashlib.sha256(seed + counter.to_bytes(8, "big")).digest(), "big")
        if value < limit:
            return low + value % span
        counter += 1


def field_value(field, seed, index):
    kind = field["type"]
    if kind == "sequence":
        return field["start"] + (index - 1) * field["step"]
    if kind == "integer":
        return draw(seed, field["min"], field["max"])
    if kind == "choice":
        return field["values"][draw(seed, 0, len(field["values"]) - 1)]
    if kind == "date":
        return date.fromordinal(draw(seed, iso(field["min"]).toordinal(), iso(field["max"]).toordinal())).isoformat()
    if kind == "boolean":
        numerator, denominator = Decimal(field["true_probability"]).as_integer_ratio()
        return int(draw(seed, 0, denominator - 1) < numerator)
    with localcontext() as context:
        context.prec = 120
        scale = Decimal(10) ** field["places"]
        value = Decimal(draw(seed, int(Decimal(field["min"]) * scale), int(Decimal(field["max"]) * scale))) / scale
        return format(value, f'.{field["places"]}f')


def records(config):
    for index in range(1, config["rows"] + 1):
        identity = compact(["1876-id-v1", config["dataset_id"], config["seed"], index])
        row = [str(uuid.uuid5(uuid.NAMESPACE_URL, identity)), index]
        for field in config["fields"]:
            seed = hashlib.sha256(compact(["1876-field-v1", config["dataset_id"], config["seed"], index, field["name"]]).encode("utf-8")).digest()
            row.append(field_value(field, seed, index))
        yield row


def csv_values(row, fields):
    result = [str(value) for value in row]
    for index, field in enumerate(fields, 2):
        if field["type"] == "choice" and result[index].lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")):
            result[index] = "'" + result[index]
    return result


def logical_line(row):
    return (compact([str(value) for value in row]) + "\n").encode("utf-8")


def write_dataset(stage, config):
    columns = ["id", "record_index", *[field["name"] for field in config["fields"]]]
    sql_types = {"sequence": "INTEGER", "integer": "INTEGER", "boolean": "INTEGER", "decimal": "TEXT", "date": "TEXT", "choice": "TEXT"}
    definitions = ['"id" TEXT PRIMARY KEY NOT NULL', '"record_index" INTEGER UNIQUE NOT NULL'] + [f'"{field["name"]}" {sql_types[field["type"]]} NOT NULL' for field in config["fields"]]
    logical, preview = hashlib.sha256(), []
    with closing(sqlite3.connect(stage / OUTPUTS["sqlite"])) as connection:
        connection.execute("CREATE TABLE records (" + ",".join(definitions) + ")")
        statement = "INSERT INTO records VALUES (" + ",".join("?" for _ in columns) + ")"
        batch = []
        with (stage / OUTPUTS["csv"]).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            for row in records(config):
                writer.writerow(csv_values(row, config["fields"]))
                logical.update(logical_line(row))
                batch.append(row)
                if len(preview) < 12:
                    preview.append(dict(zip(columns, map(str, row))))
                if len(batch) >= 1000:
                    connection.executemany(statement, batch)
                    batch.clear()
            if batch:
                connection.executemany(statement, batch)
        connection.commit()
        count, unique_ids, unique_indices = connection.execute("SELECT COUNT(*),COUNT(DISTINCT id),COUNT(DISTINCT record_index) FROM records").fetchone()
        if (count, unique_ids, unique_indices) != (config["rows"],) * 3:
            raise WorkflowError("SQLite row or uniqueness verification failed.")
        verified, verified_logical = 0, hashlib.sha256()
        with (stage / OUTPUTS["csv"]).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            if next(reader) != columns:
                raise WorkflowError("CSV header verification failed.")
            for csv_row, sql_row in itertools.zip_longest(reader, connection.execute("SELECT * FROM records ORDER BY record_index")):
                if csv_row is None or sql_row is None or csv_row != csv_values(sql_row, config["fields"]):
                    raise WorkflowError("Ordered CSV/SQLite content verification failed.")
                if uuid.UUID(sql_row[0]).version != 5:
                    raise WorkflowError("UUID version verification failed.")
                verified_logical.update(logical_line(sql_row))
                verified += 1
        if verified != count or logical.hexdigest() != verified_logical.hexdigest():
            raise WorkflowError("Logical-record checksum verification failed.")
    return {"rows": count, "unique_ids": unique_ids, "sqlite_rows": count, "verified_matches": verified,
            "verified_cells": verified * len(columns), "ordered_logical_sha256": logical.hexdigest(), "preview": preview}


def write_html(path, audit):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = "".join(f'<div class="card"><span>{label}</span><strong>{audit[key]:,}</strong></div>' for key, label in [("rows", "RECORDS"), ("unique_ids", "UNIQUE IDS"), ("sqlite_rows", "SQLITE ROWS"), ("verified_matches", "VERIFIED ROW MATCHES")])
    fields = "".join(f'<tr><td><code>{esc(field["name"])}</code></td><td><span class="type">{esc(field["type"])}</span></td><td>{esc(compact({key: value for key, value in field.items() if key not in {"name", "type"}}))}</td></tr>' for field in audit["fields"])
    columns = list(audit["preview"][0])
    preview = "".join("<tr>" + "".join(f'<td>{esc(row[name])}</td>' for name in columns) + "</tr>" for row in audit["preview"])
    hashes = "".join(f'<div class="checksum"><span>{label}</span><code>{audit[key]}</code></div>' for key, label in [("source_config_sha256", "Original config bytes"), ("canonical_config_sha256", "Canonical JSON config"), ("ordered_logical_sha256", "Ordered logical records"), ("csv_sha256", "CSV file bytes"), ("sqlite_sha256", "SQLite file bytes")])
    document = '''<!doctype html><html lang="en" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reproducible synthetic dataset audit</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f5f8;color:#18364a;font:14px/1.5 system-ui,Segoe UI,sans-serif}main{max-width:1320px;margin:auto;padding:30px 26px}header{background:#143549;color:white;border-radius:13px;padding:27px 30px}.eyebrow{color:#8cdecd;letter-spacing:.14em;font-size:11px;font-weight:750}h1{font-size:33px;line-height:1.15;margin:11px 0}header p{color:#c8dce7;margin:6px 0 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:23px 0}.card{background:white;border:1px solid #d7e4ec;border-radius:11px;padding:18px}.card span{font-size:11px;color:#6b8191;letter-spacing:.07em}.card strong{display:block;font-size:34px;color:#0b8076;margin-top:6px;font-weight:650}.note{background:#e4f3ee;border-left:4px solid #198f7d;padding:15px 18px;border-radius:5px}section{background:white;border:1px solid #d9e5ec;border-radius:12px;padding:22px;margin-top:21px}h2{font-size:20px;margin:0 0 14px}.table{overflow:auto;border:1px solid #e0e8ee;border-radius:7px}table{width:100%;border-collapse:collapse;text-align:left}th{background:#edf4f8;color:#5b778a;text-transform:uppercase;font-size:10px;white-space:nowrap}th,td{padding:10px 12px;border-bottom:1px solid #e7eef3}td{font-size:12px;max-width:380px;overflow-wrap:anywhere;vertical-align:top}code{font:11px/1.5 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}.type{color:#087d74;background:#e7f5ef;padding:3px 8px;border-radius:11px;font-size:11px}.muted{color:#698293;font-size:12px}.checksum{padding:9px 0;border-bottom:1px solid #e7edf2}.checksum span{display:block;font-size:11px;color:#698293;margin-bottom:3px}.checksum code{display:block}footer{color:#698293;font-size:11px;margin-top:23px}@media(max-width:720px){main{padding:16px}.cards{grid-template-columns:repeat(2,1fr)}h1{font-size:27px}header{padding:22px}}@media print{body{background:white}main{padding:0}header{background:white;color:#18364a;border:1px solid #a8bac7}header p{color:#617e91}section{break-inside:avoid}.table{overflow:visible}td,th{font-size:9px;padding:5px}}
</style><main><header><div class="eyebrow">SYNTHETIC DATA / REPRODUCIBLE LOGICAL RECORDS</div><h1>Configurable test data, verified in two formats</h1><p>Dataset: DATASET · Seed: SEED · Config version: 1</p></header><div class="cards">CARDS</div><div class="note"><b>Independent synthetic-data demonstration.</b> No live database or credentials are used. CSV and local SQLite records were compared in record order, field by field. Every ID is a deterministic UUIDv5 synthetic identifier; these IDs are not security tokens.</div><section><h2>Generation configuration</h2><p class="muted">Field-specific SHA256 streams keep values stable when fields are reordered. The same dataset, seed, record index, and field definition reproduce the same logical values.</p><div class="table"><table><thead><tr><th>FIELD</th><th>TYPE</th><th>PARAMETERS</th></tr></thead><tbody>FIELDS</tbody></table></div></section><section><h2>First 12 logical records</h2><p class="muted">PREVIEW_COUNT rows shown. SQLite retains original text. Formula-like choice values receive a protective apostrophe in CSV only.</p><div class="table"><table><thead><tr>HEADERS</tr></thead><tbody>PREVIEW</tbody></table></div></section><section><h2>Integrity and reproducibility</h2><p class="muted">Reproducibility applies to the ordered logical records and identifiers. Each exported file has its own byte checksum; SQLite container bytes are independently audited.</p>HASHES</section><footer>Generated UTC: GENERATED. Source configuration is unchanged. The ID identity is the compact JSON array ["1876-id-v1", dataset_id, seed, 1-based record_index], under the standard UUID URL namespace. Output files are published together only after verification.</footer></main></html>'''
    replacements = {"DATASET": esc(audit["dataset_id"]), "SEED": esc(audit["seed"]), "CARDS": cards, "FIELDS": fields, "PREVIEW_COUNT": str(len(audit["preview"])), "HEADERS": "".join(f"<th>{esc(name)}</th>" for name in columns), "PREVIEW": preview, "HASHES": hashes, "GENERATED": esc(audit["generated_at"])}
    Path(path).write_text(re.sub(r"\b(DATASET|SEED|CARDS|FIELDS|PREVIEW_COUNT|HEADERS|PREVIEW|HASHES|GENERATED)\b", lambda m: replacements[m[0]], document), encoding="utf-8")


def generate(config_path, output_dir):
    source = Path(config_path).absolute()
    if source.is_symlink():
        raise WorkflowError("Configuration must not be a symbolic link.")
    try:
        original = source.read_bytes()
        config = validate_config(json.loads(original.decode("utf-8-sig"), object_pairs_hook=reject_duplicate_keys, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON constant: " + value))))
    except WorkflowError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkflowError("Cannot read configuration: " + str(exc)) from exc
    raw = Path(output_dir).absolute()
    if raw.is_symlink():
        raise WorkflowError("Output directory must not be a symbolic link.")
    destination = raw.resolve()
    if destination == source.resolve() or destination in source.resolve().parents:
        raise WorkflowError("Output directory must not contain the source configuration.")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise WorkflowError("Output directory must be new or empty; existing files are never overwritten.")
    stage = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="1876_生成暂存_", dir=destination.parent))
        result = write_dataset(stage, config)
        audit = {"schema_version": 1, "generator_version": "1.0", "status": "complete", "dataset_id": config["dataset_id"], "seed": config["seed"], **result, "fields": config["fields"], "source_config": source.name, "source_config_sha256": sha(original), "canonical_config_sha256": sha(json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")), "config": config, "id_rule": ID_RULE, "outputs": dict(OUTPUTS), "csv_sha256": sha((stage / OUTPUTS["csv"]).read_bytes()), "sqlite_sha256": sha((stage / OUTPUTS["sqlite"]).read_bytes()), "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (stage / OUTPUTS["json"]).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        write_html(stage / OUTPUTS["html"], audit)
        if source.is_symlink() or sha(source.read_bytes()) != audit["source_config_sha256"]:
            raise WorkflowError("Source configuration changed during generation; no files were published.")
        if destination.exists():
            destination.rmdir()
        os.replace(stage, destination)
        stage = None
        return audit
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError("Generation failed; no formal output files were published: " + str(exc)) from exc
    finally:
        if stage is not None and stage.parent.resolve() == destination.parent and stage.name.startswith("1876_生成暂存_"):
            shutil.rmtree(stage)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Generate reproducible synthetic CSV and SQLite records.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = generate(args.config, args.output)
        print(json.dumps({key: result[key] for key in ("status", "rows", "unique_ids", "sqlite_rows", "verified_matches", "outputs")}, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERROR: Unexpected failure: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
