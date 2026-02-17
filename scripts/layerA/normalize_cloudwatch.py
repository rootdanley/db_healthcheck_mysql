#!/usr/bin/env python3
import argparse
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pandas as pd
import yaml


# aceita YYYY/MM/DD ou YYYY-MM-DD
DATE_LINE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2} \d{2}:\d{2}:\d{2},")
FNAME_RE = re.compile(
    r"^(?P<metric>.+?)-\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}-\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}-UTC(?P<tz>[-+]\d+)\.csv$"
)

BASE_METRICS = {
    "DBLoadRelativeToNumVCPUs",
    "CPUUtilization",
    "DatabaseConnections",
    "FreeableMemory",
    "FreeStorageSpace_Minimum",
    "ReadLatency",
    "WriteLatency",
    "ReadIOPS",
    "WriteIOPS",
    # TotalIOPS é derivado
}

SERVERLESS_ONLY = {
    "ServerlessDatabaseCapacity",
    "ACUUtilization",
}

# mínimos por modo (Aurora normalmente não tem FreeStorageSpace_Minimum)
REQUIRED_MIN_COMMUNITY = {
    "DBLoadRelativeToNumVCPUs",
    "CPUUtilization",
    "DatabaseConnections",
    "FreeableMemory",
    "FreeStorageSpace_Minimum",
}
REQUIRED_MIN_AURORA = {
    "DBLoadRelativeToNumVCPUs",
    "CPUUtilization",
    "DatabaseConnections",
    "FreeableMemory",
}


def _strip(x) -> str:
    return "" if x is None else str(x).strip()


def engine_mode_from_yaml(engine_hint: str) -> str:
    h = _strip(engine_hint).lower()
    if "aurora" in h and "serverless" in h:
        return "aurora_serverless_v2"
    if "aurora" in h:
        return "aurora"
    return "community"


def get_metric_id_from_filename(file_path: Path) -> str:
    m = FNAME_RE.match(file_path.name)
    if m:
        return m.group("metric")
    return file_path.stem.split("-", 1)[0]


def canonical_metric(metric_id: str, metric_label: str = "") -> str:
    """
    Canonicalização robusta (ignora _ - ( ) e case).
    """
    mid = _strip(metric_id)
    key = re.sub(r"[^A-Za-z0-9]", "", mid).lower()

    # mapa por "startswith" em chave normalizada
    def _starts(prefix: str) -> bool:
        return key.startswith(prefix)

    if _starts("freestoragespace"):
        return "FreeStorageSpace_Minimum"
    if _starts("dbloadrelativetonumvcpus"):
        return "DBLoadRelativeToNumVCPUs"
    if _starts("cpuutilization"):
        return "CPUUtilization"
    if _starts("databaseconnections"):
        return "DatabaseConnections"
    if _starts("freeablememory"):
        return "FreeableMemory"
    if _starts("readlatency"):
        return "ReadLatency"
    if _starts("writelatency"):
        return "WriteLatency"
    if _starts("readiops"):
        return "ReadIOPS"
    if _starts("writeiops"):
        return "WriteIOPS"
    if _starts("serverlessdatabasecapacity"):
        return "ServerlessDatabaseCapacity"
    if _starts("acuutilization"):
        return "ACUUtilization"

    lbl = _strip(metric_label).replace(" ", "")
    if lbl in BASE_METRICS or lbl in SERVERLESS_ONLY:
        return lbl

    return mid


def parse_full_label(full_label: str):
    tokens = full_label.strip().split()
    if len(tokens) < 4:
        return (None, None, None, None)

    period_sec = None
    stat = None
    try:
        period_sec = int(tokens[-1])
        stat = tokens[-2]
    except Exception:
        period_sec = None
        stat = None

    ident = None
    metric_label = None

    idx = None
    for i, t in enumerate(tokens):
        if t.startswith("DBInstanceIdentifier:"):
            idx = i
            ident = t.split("DBInstanceIdentifier:", 1)[1]
            break
        if t.startswith("DBClusterIdentifier:"):
            idx = i
            ident = t.split("DBClusterIdentifier:", 1)[1]
            break

    if idx is not None:
        metric_parts = tokens[idx + 1 : -2] if stat is not None else tokens[idx + 1 :]
        metric_label = " ".join(metric_parts).strip() if metric_parts else None

    return (ident, metric_label, stat, period_sec)


def parse_cloudwatch_export(file_path: Path, tzinfo: timezone, canonical_name: str):
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()

    full_label = None
    label_instance = None

    for ln in lines[:40]:
        if ln.startswith("Full label,"):
            full_label = ln.split(",", 1)[1].strip()
        elif ln.startswith("Label,"):
            label_instance = ln.split(",", 1)[1].strip()

    ident, metric_label, stat, period_sec = (None, None, None, None)
    if full_label:
        ident, metric_label, stat, period_sec = parse_full_label(full_label)

    if not ident:
        ident = label_instance

    records = []
    for ln in lines:
        if not DATE_LINE_RE.match(ln):
            continue

        ts_str, val_str = ln.split(",", 1)
        ts = pd.to_datetime(ts_str.strip(), errors="coerce")
        if pd.isna(ts):
            continue

        # ts vem naive no export -> localiza no tz do run
        if ts.tzinfo is None:
            ts = ts.tz_localize(tzinfo)
        else:
            ts = ts.tz_convert(tzinfo)

        val_str = val_str.strip().strip('"')
        if val_str == "" or val_str.lower() == "nan":
            value = None
        else:
            try:
                value = float(val_str)
            except Exception:
                value = None

        records.append(
            {
                "ts": ts.isoformat(),
                "instance": ident or "",
                "metric": canonical_name,
                "metric_label": metric_label or "",
                "stat": stat or "",
                "period_sec": period_sec if period_sec is not None else "",
                "value": value,
                "source_file": file_path.name,
            }
        )

    return records


def derive_total_iops_long(df_valid_ts: pd.DataFrame) -> pd.DataFrame:
    if df_valid_ts.empty:
        return df_valid_ts

    have_read = (df_valid_ts["metric"] == "ReadIOPS").any()
    have_write = (df_valid_ts["metric"] == "WriteIOPS").any()
    if not (have_read and have_write):
        return df_valid_ts

    tmp = df_valid_ts[df_valid_ts["metric"].isin(["ReadIOPS", "WriteIOPS"])].copy()
    tmp["ts_dt"] = pd.to_datetime(tmp["ts"], errors="coerce")
    tmp = tmp.dropna(subset=["ts_dt"])

    p = tmp.pivot_table(index=["ts", "instance"], columns="metric", values="value", aggfunc="mean")
    if "ReadIOPS" not in p.columns or "WriteIOPS" not in p.columns:
        return df_valid_ts

    p["TotalIOPS"] = p["ReadIOPS"].fillna(0) + p["WriteIOPS"].fillna(0)
    p = p.reset_index()[["ts", "instance", "TotalIOPS"]]

    add = pd.DataFrame({
        "ts": p["ts"],
        "instance": p["instance"],
        "metric": "TotalIOPS",
        "metric_label": "Total IOPS (ReadIOPS + WriteIOPS)",
        "stat": "DERIVADO",
        "period_sec": "",
        "value": p["TotalIOPS"],
        "source_file": "DERIVED",
    })

    return pd.concat([df_valid_ts, add], ignore_index=True)


def resolve_paths(run_yaml: Path, week: str, input_dir_arg: str, output_dir_arg: str) -> tuple[Path, Path]:
    instance_root = run_yaml.parent
    input_dir = Path(input_dir_arg) if _strip(input_dir_arg) else (instance_root / "layerA" / "inputs" / week)
    output_dir = Path(output_dir_arg) if _strip(output_dir_arg) else (instance_root / "layerA" / "collected" / week)
    return input_dir, output_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True, help="run.yaml da instância (para week/engine e paths padrão)")
    ap.add_argument("--week", default="", help="Override do week (YYYY-MM-DD)")
    ap.add_argument("--input-dir", default="", help="Override do input-dir (senão usa layerA/inputs/<week>)")
    ap.add_argument("--output-dir", default="", help="Override do output-dir (senão usa layerA/collected/<week>)")
    ap.add_argument("--tz-offset-hours", type=int, default=-3, help="Offset do timezone (ex: -3)")
    ap.add_argument("--engine", default="", help="Override do engine (ex: mysql_community, aurora_provisioned, aurora_serverless_v2)")
    ap.add_argument("--strict-min", action="store_true", help="Falha se faltar qualquer métrica do mínimo recomendado")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    week = _strip(args.week) or _strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter 'week: YYYY-MM-DD' (ou passe --week).")

    engine_hint = _strip(args.engine) or _strip(cfg.get("engine"))
    mode = engine_mode_from_yaml(engine_hint)

    allowed_metrics = set(BASE_METRICS)
    if mode == "aurora_serverless_v2":
        allowed_metrics |= set(SERVERLESS_ONLY)

    required_min = REQUIRED_MIN_COMMUNITY if mode == "community" else REQUIRED_MIN_AURORA

    input_dir, output_dir = resolve_paths(run_yaml, week, args.input_dir, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tzinfo = timezone(timedelta(hours=args.tz_offset_hours))

    files = sorted([p for p in input_dir.glob("*.csv") if p.is_file()])
    if not files:
        raise SystemExit(f"Nenhum CSV encontrado em: {input_dir}")

    included_files = []
    skipped_files = []
    all_records = []

    # datapoints por métrica (pra diagnóstico)
    metric_points = {m: 0 for m in allowed_metrics}

    for f in files:
        metric_id = get_metric_id_from_filename(f)
        mcanon = canonical_metric(metric_id)

        if mcanon not in allowed_metrics:
            skipped_files.append((f.name, metric_id, mcanon))
            continue

        recs = parse_cloudwatch_export(f, tzinfo, mcanon)
        included_files.append((f.name, metric_id, mcanon, len(recs)))
        metric_points[mcanon] = metric_points.get(mcanon, 0) + len(recs)
        all_records.extend(recs)

    df = pd.DataFrame(all_records)
    if df.empty:
        raise SystemExit("Nenhum datapoint foi extraído dos CSVs (verifique formato de timestamp do export).")

    # long
    long_path = output_dir / "cloudwatch_long.csv"
    df.to_csv(long_path, index=False)

    # wide (preserva colunas presentes mesmo que fiquem NaN)
    wide_path = output_dir / "cloudwatch_wide.csv"
    wide = df.copy()
    wide["ts"] = pd.to_datetime(wide["ts"], errors="coerce")
    wide = wide.dropna(subset=["ts"])

    wide_p = wide.pivot_table(index="ts", columns="metric", values="value", aggfunc="mean").sort_index()

    # garante colunas de métricas que realmente tiveram datapoints
    present_metrics = sorted([m for m, n in metric_points.items() if n > 0 and m in wide["metric"].unique()])
    wide_p = wide_p.reindex(columns=present_metrics)

    if "ReadIOPS" in wide_p.columns and "WriteIOPS" in wide_p.columns and "TotalIOPS" not in wide_p.columns:
        wide_p["TotalIOPS"] = wide_p["ReadIOPS"].fillna(0) + wide_p["WriteIOPS"].fillna(0)

    wide_p.to_csv(wide_path)

    # summary
    summary_path = output_dir / "cloudwatch_summary.txt"
    seen_metrics = set([m for m, n in metric_points.items() if n > 0])
    missing_required = sorted(required_min - seen_metrics)

    with open(summary_path, "w", encoding="utf-8") as w:
        w.write("CloudWatch Normalização — Resumo (PT-BR)\n")
        w.write("=================================================\n")
        w.write(f"run_yaml={run_yaml}\n")
        w.write(f"week={week}\n")
        w.write(f"input_dir={input_dir}\n")
        w.write(f"output_dir={output_dir}\n")
        w.write(f"engine_hint={engine_hint or 'N/A'}\n")
        w.write(f"modo_detectado={mode}\n\n")

        w.write("Datapoints por métrica (canônica):\n")
        for m in sorted(allowed_metrics):
            w.write(f"  - {m}: {metric_points.get(m, 0)}\n")

        w.write("\nArquivos incluídos:\n")
        for fname, mid, canon, nrecs in included_files:
            w.write(f"  - {fname} | metric_id={mid} | canon={canon} | datapoints={nrecs}\n")

        w.write("\nArquivos ignorados:\n")
        for fname, mid, canon in skipped_files:
            w.write(f"  - {fname} | metric_id={mid} | canon={canon}\n")

        if missing_required:
            w.write("\nATENÇÃO: faltando métricas mínimas recomendadas:\n")
            for m in missing_required:
                w.write(f"  - {m}\n")

        w.write(f"\nwide_gerado=sim ({wide_path.name})\n")

    if args.strict_min and missing_required:
        raise SystemExit("Falha (strict-min): faltando métricas mínimas: " + ", ".join(missing_required))

    print(f"[OK] cloudwatch_long:    {long_path}")
    print(f"[OK] cloudwatch_wide:    {wide_path}")
    print(f"[OK] cloudwatch_summary: {summary_path}")


if __name__ == "__main__":
    main()
