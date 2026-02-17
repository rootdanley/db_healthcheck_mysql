#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import datetime as dt
import json
import re
from pathlib import Path

import yaml

# -----------------------------
# Utils
# -----------------------------
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

YESNO_FIELDS = {
    "RDS Extended Support (Enabled/Disabled)",
    "Auto minor version upgrade (Yes/No)",
    "Secondary AZ",
    "Publicly accessible (Yes/No)",
    "Deletion protection (Enabled/Disabled)",
    "Enhanced Monitoring (Enabled/Disabled)",
    "Monitoring type (Database Insights)",
    "Performance Insights enabled (Yes/No)",
    "Storage autoscaling (Enabled/Disabled)",
}

def as_str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return str(x)

def strip(x) -> str:
    return as_str(x).strip()

def norm_text(x: str) -> str:
    s = strip(x)
    s = s.replace("NÃ£o", "Nao").replace("NÃo", "Nao")
    s = s.replace("Não", "Nao").replace("NÃO", "Nao")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def norm_yes_no(x: str) -> str:
    s = norm_text(x).lower()
    if s in ("sim", "yes", "y", "true", "1", "enabled", "on", "ativo", "habilitado"):
        return "Yes"
    if s in ("nao", "não", "no", "n", "false", "0", "disabled", "off", "inativo", "desabilitado"):
        return "No"
    return strip(x)

def first_nonempty(*vals: str) -> str:
    for v in vals:
        s = strip(v)
        if s:
            return s
    return ""

def parse_date_flexible(x: str):
    s = strip(x)
    if not s:
        return None

    s = s.replace(",", " ").strip()
    s = re.sub(r"\s+", " ", s)

    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:
        pass

    m = re.match(r"^(\d{1,2})-([A-Za-z]{3,9})-(\d{4})$", s)
    if m:
        d = int(m.group(1))
        mon = MONTHS.get(m.group(2).lower()[:3], None)
        y = int(m.group(3))
        if mon:
            return dt.date(y, mon, d)

    m = re.match(r"^(\d{1,2}) ([A-Za-z]{3,9}) (\d{4})$", s)
    if m:
        d = int(m.group(1))
        mon = MONTHS.get(m.group(2).lower(), MONTHS.get(m.group(2).lower()[:3], None))
        y = int(m.group(3))
        if mon:
            return dt.date(y, mon, d)

    return None

def days_to(d: dt.date | None, today: dt.date):
    if not d:
        return ""
    return (d - today).days

def read_csv_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        return [row for row in rdr]

def write_rows_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

def write_single_row_csv(path: Path, row: dict, fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})

# -----------------------------
# Inventory parsing
# -----------------------------
def read_inventory_rows(inventory_csv: Path) -> list[dict]:
    rows = read_csv_rows(inventory_csv)
    if not rows:
        raise ValueError(f"Inventory vazio: {inventory_csv}")
    for c in ("secao", "campo", "primary"):
        if c not in rows[0]:
            raise ValueError(f"Inventory {inventory_csv} não tem coluna '{c}'. Esperado: secao,campo,primary")
    return rows

def rows_to_kv(rows: list[dict]) -> tuple[dict, dict]:
    kv = {}
    secao_map = {}
    for r in rows:
        secao = norm_text(r.get("secao", ""))
        campo = norm_text(r.get("campo", ""))
        val = norm_text(r.get("primary", ""))

        if not campo:
            continue

        if campo in YESNO_FIELDS:
            val = norm_yes_no(val)
        else:
            val = strip(val)

        kv[campo] = val
        if campo not in secao_map:
            secao_map[campo] = secao or "Outros"
    return kv, secao_map

# -----------------------------
# Deployment detection
# -----------------------------
def looks_like_aurora(engine: str, engine_version: str) -> bool:
    e = strip(engine).lower()
    v = strip(engine_version).lower()
    return ("aurora" in e) or ("mysql_aurora" in v)

def is_serverless_v2(instance_class: str) -> bool:
    return strip(instance_class).lower() == "db.serverless"

def detect_deployment_type(kv: dict, engine_hint: str = "") -> str:
    """
    Retorna:
      - rds_mysql_community
      - aurora_provisioned
      - aurora_serverless_v2
    """
    hint = strip(engine_hint).lower()

    ic = strip(kv.get("Instance class", "")).lower()
    if is_serverless_v2(ic):
        return "aurora_serverless_v2"

    engine = strip(kv.get("Engine", ""))
    engine_version = strip(kv.get("Engine version", ""))

    if hint in ("aurora", "aurora_mysql", "aurora-mysql", "aurora_serverless_v2", "aurora_provisioned"):
        return "aurora_provisioned"

    if looks_like_aurora(engine, engine_version):
        return "aurora_provisioned"

    return "rds_mysql_community"

def infer_engine_label(kv: dict, deployment_type: str) -> str:
    eng = strip(kv.get("Engine", ""))
    if eng:
        return eng
    if deployment_type.startswith("aurora_"):
        return "Aurora MySQL (aurora-mysql)"
    return "MySQL Community"

# -----------------------------
# Normalization rules (KV)
# -----------------------------
def normalize_kv(kv: dict, deployment_type: str) -> dict:
    for k in list(kv.keys()):
        if k in YESNO_FIELDS:
            kv[k] = norm_yes_no(kv[k])
        else:
            kv[k] = strip(kv[k])

    kv["Engine"] = infer_engine_label(kv, deployment_type)

    # Identidade mínima
    kv.setdefault("DB name", "")
    kv.setdefault("DB identifier", "")

    # Enhanced Monitoring interval empty if disabled
    if kv.get("Enhanced Monitoring (Enabled/Disabled)") == "No":
        kv["Enhanced Monitoring interval (sec)"] = ""

    # Serverless v2: normaliza ACU decimal
    if deployment_type == "aurora_serverless_v2":
        kv["Instance class"] = "db.serverless"
        if strip(kv.get("Min ACU", "")):
            kv["Min ACU"] = strip(kv["Min ACU"]).replace(",", ".")
        if strip(kv.get("Max ACU", "")):
            kv["Max ACU"] = strip(kv["Max ACU"]).replace(",", ".")
        kv.setdefault("Min ACU", "")
        kv.setdefault("Max ACU", "")

    return kv

# -----------------------------
# Normalized inventory rows (secao,campo,primary)
# IMPORTANT: NÃO “inventar” coisas que não existem no Aurora
# -----------------------------
def build_normalized_inventory_rows(rows_in: list[dict], kv: dict, secao_map: dict, deployment_type: str) -> list[dict]:
    seen = set()
    out = []

    # 1) Regrava apenas o que existe no input (com valores normalizados)
    for r in rows_in:
        secao = norm_text(r.get("secao", "")) or "Outros"
        campo = norm_text(r.get("campo", ""))
        if not campo:
            continue
        seen.add(campo)
        secao_map.setdefault(campo, secao)
        out.append({"secao": secao_map.get(campo, secao), "campo": campo, "primary": kv.get(campo, "")})

    def ensure_row(secao: str, campo: str, value: str, only_if_value: bool = False):
        c = norm_text(campo)
        if c in seen:
            return
        v = strip(value)
        if only_if_value and not v:
            return
        seen.add(c)
        out.append({"secao": secao, "campo": c, "primary": v})

    # 2) Campos “mínimos” e úteis (sem criar lixo de Aurora)
    ensure_row("Topologia", "Deployment type", deployment_type, only_if_value=False)

    # DB identifier: para Aurora a gente pode inferir pelo DB name (sem “inventar” campos de storage)
    inferred_id = first_nonempty(kv.get("DB identifier", ""), kv.get("DB name", ""))
    ensure_row("Identidade", "DB identifier", inferred_id, only_if_value=True)

    # Engine: se não existia no input, vale colocar (é real: engine existe)
    ensure_row("Engine & suporte", "Engine", kv.get("Engine", ""), only_if_value=True)

    # Compute: só adiciona vCPU/RAM se fizer sentido
    if deployment_type in ("rds_mysql_community", "aurora_provisioned"):
        ensure_row("Compute", "vCPU", kv.get("vCPU", ""), only_if_value=True)
        ensure_row("Compute", "RAM (GiB)", kv.get("RAM (GiB)", ""), only_if_value=True)

    # Serverless v2: garante Min/Max ACU se não existiam
    if deployment_type == "aurora_serverless_v2":
        ensure_row("Compute", "Min ACU", kv.get("Min ACU", ""), only_if_value=True)
        ensure_row("Compute", "Max ACU", kv.get("Max ACU", ""), only_if_value=True)

    # Grupos: Aurora tem cluster+instance; RDS tem parameter+option
    if deployment_type.startswith("aurora_"):
        ensure_row("Grupos", "DB cluster parameter group", kv.get("DB cluster parameter group", ""), only_if_value=True)
        ensure_row("Grupos", "DB instance parameter group", kv.get("DB instance parameter group", ""), only_if_value=True)
    else:
        ensure_row("Grupos", "DB parameter group", kv.get("DB parameter group", ""), only_if_value=True)
        ensure_row("Grupos", "Option group", kv.get("Option group", ""), only_if_value=True)

    # Storage: NÃO adicionar allocated/iops/throughput para Aurora.
    # Para Aurora, o que existe no input fica; se não existe, não forçamos nada.
    # Para RDS community, idem: não criamos campo novo de storage, só regravamos o que veio no input.
    return out

# -----------------------------
# Support calendars helpers
# -----------------------------
def load_calendar(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return read_csv_rows(path)

def pick_first(rows: list[dict], predicate):
    for r in rows:
        if predicate(r):
            return r
    return None

def extract_mysql_major(engine_version: str) -> str:
    m = re.match(r"^\s*(\d+)\.(\d+)\.", strip(engine_version))
    if not m:
        return ""
    return f"MySQL {m.group(1)}.{m.group(2)}"

def extract_aurora_minor(engine_version: str) -> str:
    s = strip(engine_version)
    m = re.search(r"mysql_aurora\.(\d+)\.(\d+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2).zfill(2)}"
    return ""

def aurora_major_label(aurora_minor: str) -> str:
    s = strip(aurora_minor)
    if not s or "." not in s:
        return ""
    major = s.split(".", 1)[0]
    return f"Aurora MySQL {major}"

def compute_support(kv: dict, support_dir: Path, today: dt.date, deployment_type: str) -> dict:
    out = {
        "engine_family": "Aurora MySQL" if deployment_type.startswith("aurora_") else "RDS MySQL Community",
        "extended_support_enabled": norm_yes_no(kv.get("RDS Extended Support (Enabled/Disabled)", "")),
        "minor_key": "",
        "major_key": "",
        "compatible_mysql": "",
        "minor_end_standard": "",
        "minor_days_to_end_standard": "",
        "major_end_standard": "",
        "major_days_to_end_standard": "",
        "ext_year1_pricing_start": "",
        "days_to_ext_year1_pricing_start": "",
        "ext_year3_pricing_start": "",
        "days_to_ext_year3_pricing_start": "",
        "ext_end": "",
        "days_to_ext_end": "",
    }

    engine_version = strip(kv.get("Engine version", ""))

    cm_major = load_calendar(support_dir / "support_calendar_major_mysql_rds.csv")
    cm_minor = load_calendar(support_dir / "support_calendar_minor_mysql_rds.csv")
    au_major = load_calendar(support_dir / "support_calendar_major_aurora_mysql_rds.csv")
    au_minor = load_calendar(support_dir / "support_calendar_minor_aurora_mysql_rds.csv")

    if deployment_type.startswith("aurora_"):
        minor_key = extract_aurora_minor(engine_version)
        major_key = aurora_major_label(minor_key)
        out["minor_key"] = minor_key
        out["major_key"] = major_key

        rmin = pick_first(au_minor, lambda r: strip(r.get("aurora_mysql_version")) == minor_key)
        if rmin:
            out["compatible_mysql"] = strip(rmin.get("compatible_mysql"))
            d = parse_date_flexible(rmin.get("end_of_standard_support_date"))
            out["minor_end_standard"] = d.isoformat() if d else ""
            out["minor_days_to_end_standard"] = days_to(d, today)

        rmaj = pick_first(au_major, lambda r: strip(r.get("aurora_major_version")) == major_key)
        if rmaj:
            d = parse_date_flexible(rmaj.get("aurora_end_of_standard_support_date"))
            out["major_end_standard"] = d.isoformat() if d else ""
            out["major_days_to_end_standard"] = days_to(d, today)

            y1 = parse_date_flexible(rmaj.get("extended_support_year1_pricing_start_date"))
            y3 = parse_date_flexible(rmaj.get("extended_support_year3_pricing_start_date"))
            ee = parse_date_flexible(rmaj.get("end_of_extended_support_date"))

            out["ext_year1_pricing_start"] = y1.isoformat() if y1 else ""
            out["days_to_ext_year1_pricing_start"] = days_to(y1, today)
            out["ext_year3_pricing_start"] = y3.isoformat() if y3 else ""
            out["days_to_ext_year3_pricing_start"] = days_to(y3, today)
            out["ext_end"] = ee.isoformat() if ee else ""
            out["days_to_ext_end"] = days_to(ee, today)

    else:
        minor_key = engine_version
        major_key = extract_mysql_major(engine_version)
        out["minor_key"] = minor_key
        out["major_key"] = major_key

        rmin = pick_first(cm_minor, lambda r: strip(r.get("engine_version")) == minor_key)
        if rmin:
            d = parse_date_flexible(rmin.get("end_of_standard_support_date"))
            out["minor_end_standard"] = d.isoformat() if d else ""
            out["minor_days_to_end_standard"] = days_to(d, today)

        rmaj = pick_first(cm_major, lambda r: strip(r.get("mysql_major_version")) == major_key)
        if rmaj:
            d = parse_date_flexible(rmaj.get("rds_end_of_standard_support_date"))
            out["major_end_standard"] = d.isoformat() if d else ""
            out["major_days_to_end_standard"] = days_to(d, today)

            y1 = parse_date_flexible(rmaj.get("rds_extended_support_year1_pricing_start"))
            y3 = parse_date_flexible(rmaj.get("rds_extended_support_year3_pricing_start"))
            ee = parse_date_flexible(rmaj.get("rds_end_of_extended_support_date"))

            out["ext_year1_pricing_start"] = y1.isoformat() if y1 else ""
            out["days_to_ext_year1_pricing_start"] = days_to(y1, today)
            out["ext_year3_pricing_start"] = y3.isoformat() if y3 else ""
            out["days_to_ext_year3_pricing_start"] = days_to(y3, today)
            out["ext_end"] = ee.isoformat() if ee else ""
            out["days_to_ext_end"] = days_to(ee, today)

    return out

def compute_warnings(support: dict) -> tuple[str, str]:
    reasons = []
    level = "OK"

    def bump(new_level: str):
        nonlocal level
        order = {"OK": 0, "WARNING": 1, "CRITICAL": 2}
        if order[new_level] > order[level]:
            level = new_level

    md = support.get("minor_days_to_end_standard")
    if isinstance(md, int):
        if md <= 30:
            bump("CRITICAL"); reasons.append(f"Minor support ends in {md}d")
        elif md <= 90:
            bump("WARNING"); reasons.append(f"Minor support ends in {md}d")

    Md = support.get("major_days_to_end_standard")
    if isinstance(Md, int):
        if Md <= 90:
            bump("CRITICAL"); reasons.append(f"Major standard support ends in {Md}d")
        elif Md <= 180:
            bump("WARNING"); reasons.append(f"Major standard support ends in {Md}d")

    y1d = support.get("days_to_ext_year1_pricing_start")
    if support.get("extended_support_enabled") == "Yes" and isinstance(y1d, int):
        if y1d <= 30:
            bump("WARNING"); reasons.append(f"Extended Support year1 pricing starts in {y1d}d")

    return level, "; ".join(reasons)

# -----------------------------
# Dynamic collected header
# -----------------------------
def collected_fieldnames(deployment_type: str) -> list[str]:
    base = [
        "instance","week","deployment_type",
        "db_identifier","db_name","role",
        "engine","engine_version",
        "extended_support_enabled","auto_minor_version_upgrade",
        "instance_class",

        "master_username","publicly_accessible","deletion_protection",
        "backup_retention_days",
        "monitoring_type","performance_insights_enabled","performance_insights_retention_days",
        "enhanced_monitoring_enabled","enhanced_monitoring_interval_sec",
        "logs_exported_cloudwatch",

        "engine_family","minor_key","major_key","compatible_mysql",
        "minor_end_of_standard_support_date","minor_days_to_end_of_standard_support",
        "major_end_of_standard_support_date","major_days_to_end_of_standard_support",
        "extended_support_year1_pricing_start_date","days_to_extended_support_year1_pricing_start",
        "extended_support_year3_pricing_start_date","days_to_extended_support_year3_pricing_start",
        "end_of_extended_support_date","days_to_end_of_extended_support",
        "warning_level","warning_reasons",

        # se você não quiser esse campo, pode remover daqui e do row também
        "inventory_json",
    ]

    if deployment_type == "rds_mysql_community":
        # ✅ RDS: inclui storage + grupos (parameter/option)
        return base[:base.index("instance_class")+1] + [
            "vcpu","ram_gib",
            "storage_type","allocated_storage_gib","provisioned_iops","storage_throughput_mib",
            "storage_autoscaling","max_storage_threshold_gib",

            # ✅ AQUI é o fix:
            "db_parameter_group","option_group",
        ] + base[base.index("master_username"):]
    if deployment_type == "aurora_provisioned":
        return base[:base.index("instance_class")+1] + [
            "vcpu","ram_gib",
            "db_cluster_parameter_group","db_instance_parameter_group",
        ] + base[base.index("master_username"):]
    # aurora_serverless_v2
    return base[:base.index("instance_class")+1] + [
        "min_acu","max_acu",
        "db_cluster_parameter_group","db_instance_parameter_group",
    ] + base[base.index("master_username"):]


# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True)
    ap.add_argument("--shared-support-dir", required=True)
    ap.add_argument("--week", default="")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = strip(cfg.get("instance")) or run_yaml.parent.name
    week = strip(args.week) or strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter 'week: YYYY-MM-DD' (ou passe --week).")

    inventory_path = Path(cfg["layer0"]["inventory_csv"])
    support_dir = Path(args.shared_support_dir)

    instance_root = run_yaml.parent
    out_dir = instance_root / "layer0" / "collected" / week
    out_norm_inventory = out_dir / "layer0_inventory_normalized.csv"
    out_collected = out_dir / "layer0_collected.csv"

    rows_in = read_inventory_rows(inventory_path)
    kv_raw, secao_map = rows_to_kv(rows_in)

    engine_hint = strip(cfg.get("engine", ""))
    deployment_type = detect_deployment_type(kv_raw, engine_hint=engine_hint)
    kv = normalize_kv(kv_raw, deployment_type)

    # Normalized inventory (sem inventar storage gp3 em Aurora)
    rows_out = build_normalized_inventory_rows(rows_in, kv, secao_map, deployment_type)
    write_rows_csv(out_norm_inventory, rows_out, fieldnames=["secao","campo","primary"])

    # Support + warnings
    today = dt.date.today()
    support = compute_support(kv, support_dir, today, deployment_type)
    warning_level, warning_reasons = compute_warnings(support)

    # Single-row collected (header dinâmico)
    db_identifier = first_nonempty(kv.get("DB identifier", ""), kv.get("DB name", ""))
    row = {
        "instance": instance,
        "week": week,
        "deployment_type": deployment_type,

        "db_identifier": db_identifier,
        "db_name": strip(kv.get("DB name")),
        "role": strip(kv.get("Role (Primary/Replica)")),

        "engine": strip(kv.get("Engine")),
        "engine_version": strip(kv.get("Engine version")),

        "extended_support_enabled": strip(kv.get("RDS Extended Support (Enabled/Disabled)")),
        "auto_minor_version_upgrade": strip(kv.get("Auto minor version upgrade (Yes/No)")),

        "instance_class": strip(kv.get("Instance class")),
        "vcpu": strip(kv.get("vCPU")),
        "ram_gib": strip(kv.get("RAM (GiB)")),
        "min_acu": strip(kv.get("Min ACU")),
        "max_acu": strip(kv.get("Max ACU")),

        # RDS-only storage fields (só vão para o CSV se o header incluir)
        "storage_type": strip(kv.get("Storage type (gp3/gp2/io1)")),
        "allocated_storage_gib": strip(kv.get("Allocated storage (GiB)")),
        "provisioned_iops": strip(kv.get("Provisioned IOPS")),
        "storage_throughput_mib": strip(kv.get("Storage throughput (MiB/s)")),
        "storage_autoscaling": strip(kv.get("Storage autoscaling (Enabled/Disabled)")),
        "max_storage_threshold_gib": strip(kv.get("Max storage threshold (GiB)")),

        "master_username": strip(kv.get("Master username")),
        "publicly_accessible": strip(kv.get("Publicly accessible (Yes/No)")),
        "deletion_protection": strip(kv.get("Deletion protection (Enabled/Disabled)")),
        "backup_retention_days": strip(kv.get("Backup retention (days)")),

        "monitoring_type": strip(kv.get("Monitoring type (Database Insights)")),
        "performance_insights_enabled": strip(kv.get("Performance Insights enabled (Yes/No)")),
        "performance_insights_retention_days": strip(kv.get("Performance Insights retention (days)")),
        "enhanced_monitoring_enabled": strip(kv.get("Enhanced Monitoring (Enabled/Disabled)")),
        "enhanced_monitoring_interval_sec": strip(kv.get("Enhanced Monitoring interval (sec)")),
        "logs_exported_cloudwatch": strip(kv.get("Logs exported to CloudWatch (slow/audit/error/general)")),

        # Aurora groups
        "db_cluster_parameter_group": strip(kv.get("DB cluster parameter group")),
        "db_instance_parameter_group": strip(kv.get("DB instance parameter group")),
        # RDS groups (só entram se header incluir)
        "db_parameter_group": strip(kv.get("DB parameter group")),
        "option_group": strip(kv.get("Option group")),

        "engine_family": support["engine_family"],
        "minor_key": support["minor_key"],
        "major_key": support["major_key"],
        "compatible_mysql": support.get("compatible_mysql", ""),

        "minor_end_of_standard_support_date": support["minor_end_standard"],
        "minor_days_to_end_of_standard_support": support["minor_days_to_end_standard"],
        "major_end_of_standard_support_date": support["major_end_standard"],
        "major_days_to_end_of_standard_support": support["major_days_to_end_standard"],

        "extended_support_year1_pricing_start_date": support["ext_year1_pricing_start"],
        "days_to_extended_support_year1_pricing_start": support["days_to_ext_year1_pricing_start"],
        "extended_support_year3_pricing_start_date": support["ext_year3_pricing_start"],
        "days_to_extended_support_year3_pricing_start": support["days_to_ext_year3_pricing_start"],
        "end_of_extended_support_date": support["ext_end"],
        "days_to_end_of_extended_support": support["days_to_ext_end"],

        "warning_level": warning_level,
        "warning_reasons": warning_reasons,

        "inventory_json": json.dumps(kv, ensure_ascii=False),
    }

    fieldnames = collected_fieldnames(deployment_type)
    write_single_row_csv(out_collected, row, fieldnames=fieldnames)

    print(f"[OK] normalized inventory: {out_norm_inventory}")
    print(f"[OK] layer0 collected:      {out_collected} (dynamic schema: {deployment_type})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
