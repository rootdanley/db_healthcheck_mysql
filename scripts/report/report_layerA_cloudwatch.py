#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import csv
import datetime as dt
import io
import math
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import yaml
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# Métricas alvo (por engine)
# ============================================================

# “mínimo” comum (tanto RDS quanto Aurora)
BASE_METRICS = [
    "DBLoadRelativeToNumVCPUs",
    "CPUUtilization",
    "DatabaseConnections",
    "FreeableMemory",
    "ReadLatency",
    "WriteLatency",
]

# Só faz sentido em RDS (quando você exporta)
RDS_ONLY_METRICS = [
    "FreeStorageSpace_Minimum",  # (nome canônico esperado no wide)
    "ReadIOPS",
    "WriteIOPS",
    # TotalIOPS é derivado (Read+Write)
]

# Aurora Serverless v2 (se você exportar essas duas)
AURORA_SERVERLESS_EXTRA = [
    "ServerlessDatabaseCapacity",
    "ACUUtilization",
]

# Aliases (para evitar “Cobertura faltando” quando o wide vier com variação)
ALIASES = {
    "FreeStorageSpace_Minimum": [
        "FreeStorageSpace_Minimum",
        "FreeStorageSpace_(Minimum)",
        "FreeStorageSpace_(_Minimum_)",
        "FreeStorageSpace",
    ],
    "CPUUtilization": ["CPUUtilization", "CPUUtilization_(Average)", "CPUUtilization_(_Average_)"],
    "DBLoadRelativeToNumVCPUs": ["DBLoadRelativeToNumVCPUs"],
    "DatabaseConnections": ["DatabaseConnections"],
    "FreeableMemory": ["FreeableMemory"],
    "ReadLatency": ["ReadLatency"],
    "WriteLatency": ["WriteLatency"],
    "ReadIOPS": ["ReadIOPS"],
    "WriteIOPS": ["WriteIOPS"],
    "ServerlessDatabaseCapacity": ["ServerlessDatabaseCapacity"],
    "ACUUtilization": ["ACUUtilization"],
}


# ============================================================
# Utils
# ============================================================

def _strip(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return str(x).strip()


def _safe_float(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if not s:
            return None
        s = s.replace("\xa0", " ").replace(" ", "")
        s = s.replace(",", ".")
        return float(s)
    except Exception:
        return None


def _read_single_row_csv(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            return row
    return {}


def _html_escape(s: str) -> str:
    s = _strip(s)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def _b_to_gib(x):
    return x / (1024**3)


def _sec_to_ms(x):
    return x * 1000.0


def _fmt_num(v, unit=""):
    # N/A quando não existe ou é NaN
    if v is None:
        return "N/A"
    try:
        if isinstance(v, float) and math.isnan(v):
            return "N/A"
    except Exception:
        pass

    try:
        fv = float(v)
        if math.isnan(fv):
            return "N/A"
        if abs(fv) >= 1000:
            s = f"{fv:,.0f}"
        elif abs(fv) >= 10:
            s = f"{fv:,.2f}"
        else:
            s = f"{fv:,.3f}"
        s = s.replace(",", ".")
        return s + (f" {unit}" if unit else "")
    except Exception:
        return _strip(v) + (f" {unit}" if unit else "")


def _pick_col(df: pd.DataFrame, key: str) -> Optional[str]:
    for c in ALIASES.get(key, [key]):
        if c in df.columns:
            return c
    return None


def _df_to_html_table(df: pd.DataFrame, title: str) -> str:
    if df is None or df.empty:
        return ""
    head = "".join([f"<th>{_html_escape(c)}</th>" for c in df.columns])
    rows = []
    for _, r in df.iterrows():
        tds = "".join([f"<td>{_html_escape(r[c])}</td>" for c in df.columns])
        rows.append(f"<tr>{tds}</tr>")
    return f"""
    <div class="card">
      <div class="card-title">{_html_escape(title)}</div>
      <div class="tablewrap">
        <table class="tbl">
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </div>
    """


def _png_bytes_to_data_uri(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _save_plot_png_bytes(df, col, title, ylabel, transform=None) -> bytes:
    x = df["ts"]
    y = pd.to_numeric(df[col], errors="coerce")
    if transform:
        y = transform(y)

    plt.figure(figsize=(10, 3.6))
    plt.plot(x, y)
    plt.title(title)
    plt.xlabel("tempo")
    plt.ylabel(ylabel)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150)
    plt.close()
    return buf.getvalue()


# ============================================================
# Datas / timezone
# ============================================================

def _parse_ts(df: pd.DataFrame) -> pd.DataFrame:
    ts_col = "ts" if "ts" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
    if not ts_col:
        raise ValueError("CSV CloudWatch não tem coluna 'ts' ou 'timestamp'.")

    df = df.copy()
    df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")
    df = df.dropna(subset=[ts_col]).sort_values(ts_col)
    df = df.rename(columns={ts_col: "ts"})

    # Padroniza tz-awareness:
    # - se vier tz-naive, fixa -03:00 (padrão do seu projeto)
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(dt.timezone(dt.timedelta(hours=-3)))

    return df


def _nearest_row(df: pd.DataFrame, when: pd.Timestamp, window_minutes: int = 5) -> Optional[pd.Series]:
    if df.empty:
        return None

    ts = df["ts"]
    tz = ts.dt.tz
    w = pd.Timestamp(when)

    # garantir que "when" e "ts" tenham tz compatível
    if tz is not None:
        if w.tzinfo is None:
            w = w.tz_localize(tz)
        else:
            try:
                w = w.tz_convert(tz)
            except Exception:
                # fallback: remove tz dos dois (último recurso)
                ts = ts.dt.tz_convert(None)
                w = w.tz_convert(None)

    max_delta = pd.Timedelta(minutes=window_minutes)
    delta = (ts - w).abs()
    idx = delta.idxmin()
    if pd.isna(idx):
        return None
    if delta.loc[idx] > max_delta:
        return None
    return df.loc[idx]


# ============================================================
# KPIs / heurísticas
# ============================================================

def _series_stats(series: pd.Series) -> dict:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"min": None, "avg": None, "p95": None, "max": None}
    return {
        "min": float(s.min()),
        "avg": float(s.mean()),
        "p95": float(s.quantile(0.95)),
        "max": float(s.max()),
    }


def _kpi_row(name, series, unit="", transform=None):
    s = pd.to_numeric(series, errors="coerce")
    if transform:
        s = transform(s)
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"métrica": name, "min": "N/A", "avg": "N/A", "p95": "N/A", "max": "N/A", "unidade": unit}

    return {
        "métrica": name,
        "min": float(s.min()),
        "avg": float(s.mean()),
        "p95": float(s.quantile(0.95)),
        "max": float(s.max()),
        "unidade": unit,
    }


def _heuristics(df: pd.DataFrame, l0: dict, is_rds: bool, is_aurora: bool) -> Tuple[List[str], List[str]]:
    """
    - Mantém checks para RDS
    - Remove ruído de “ram_gib ausente” quando for Aurora
    """
    warnings: List[str] = []
    missing: List[str] = []

    allocated_gib = _safe_float(l0.get("allocated_storage_gib"))
    max_storage_threshold_gib = _safe_float(l0.get("max_storage_threshold_gib"))
    ram_gib = _safe_float(l0.get("ram_gib"))
    vcpu = _safe_float(l0.get("vcpu"))
    provisioned_iops = _safe_float(l0.get("provisioned_iops"))

    # CPU
    cpu_col = _pick_col(df, "CPUUtilization")
    if cpu_col:
        st = _series_stats(df[cpu_col])
        if st["p95"] is not None and st["p95"] >= 70:
            warnings.append(f"CPU alta: p95={st['p95']:.2f}% (sugere pressão de CPU ou queries custosas).")
        if st["max"] is not None and st["max"] >= 90:
            warnings.append(f"Picos de CPU muito altos: max={st['max']:.2f}%.")
    else:
        missing.append("CPUUtilization")

    # DBLoad/vCPU
    if "DBLoadRelativeToNumVCPUs" in df.columns:
        st = _series_stats(df["DBLoadRelativeToNumVCPUs"])
        if st["p95"] is not None and st["p95"] >= 1.0:
            warnings.append(f"DBLoadRelativeToNumVCPUs alto: p95={st['p95']:.2f} (>=1 indica saturação frequente).")
        if vcpu:
            warnings.append(f"Contexto: vCPU (Camada 0) = {int(vcpu)}. Regra prática: AAS/vCPU > 1 = fila.")
    else:
        missing.append("DBLoadRelativeToNumVCPUs")

    # Conexões
    if "DatabaseConnections" in df.columns:
        st = _series_stats(df["DatabaseConnections"])
        if st["max"] is not None and st["max"] >= 2000:
            warnings.append(f"Conexões muito altas: max={st['max']:.0f} (ver pool, leaks, bursts).")
    else:
        missing.append("DatabaseConnections")

    # Memória (%): só reclama de ram_gib ausente se NÃO for Aurora
    if "MemFreePct" in df.columns:
        st = _series_stats(df["MemFreePct"])
        if st["min"] is not None and st["min"] <= 10:
            warnings.append(f"Pouca memória livre: MemFreePct min={st['min']:.2f}% (confirmar cache/buffer/OS).")
    else:
        if "FreeableMemory" not in df.columns:
            missing.append("FreeableMemory")
        elif (not ram_gib) and (not is_aurora):
            warnings.append("RAM total (ram_gib) não encontrada na Camada 0; não foi possível calcular MemFreePct.")

    # Storage (só RDS)
    if is_rds:
        free_storage_col = _pick_col(df, "FreeStorageSpace_Minimum")
        if free_storage_col and allocated_gib:
            free_b = pd.to_numeric(df[free_storage_col], errors="coerce").dropna()
            if not free_b.empty:
                alloc_b = allocated_gib * (1024**3)
                free_min = float(free_b.min())
                pct_free_alloc = 100.0 * free_min / alloc_b if alloc_b > 0 else None
                if pct_free_alloc is not None and pct_free_alloc <= 15:
                    warnings.append(f"Pouco espaço livre (vs alocado): min={pct_free_alloc:.2f}% (risco operacional).")

                if max_storage_threshold_gib:
                    max_b = max_storage_threshold_gib * (1024**3)
                    pct_free_max = 100.0 * free_min / max_b if max_b > 0 else None
                    if pct_free_max is not None and pct_free_max <= 10:
                        warnings.append(f"Pouco espaço livre (vs MAX autoscaling): min={pct_free_max:.2f}% (próximo do limite).")
        else:
            if not free_storage_col:
                missing.append("FreeStorageSpace_Minimum")
            elif not allocated_gib:
                warnings.append("Storage alocado (Camada 0) ausente; não foi possível calcular % de espaço livre.")

    # IOPS vs provisionado (só RDS; TotalIOPS pode ser derivado)
    if is_rds:
        if "TotalIOPS" in df.columns and provisioned_iops:
            st = _series_stats(df["TotalIOPS"])
            if st["p95"] is not None:
                pct = 100.0 * st["p95"] / provisioned_iops if provisioned_iops > 0 else None
                if pct is not None and pct >= 80:
                    warnings.append(f"IOPS alto vs provisionado: TotalIOPS p95={st['p95']:.0f} (~{pct:.0f}% de {provisioned_iops:.0f}).")
        else:
            # só marca como “missing” se nem Read/Write existirem para derivar
            if "TotalIOPS" not in df.columns and ("ReadIOPS" not in df.columns or "WriteIOPS" not in df.columns):
                missing.append("TotalIOPS (derivado: ReadIOPS + WriteIOPS)")

    # Latências (segundos -> threshold 20ms = 0.02s)
    for m in ("ReadLatency", "WriteLatency"):
        if m in df.columns:
            st = _series_stats(df[m])
            if st["p95"] is not None and st["p95"] >= 0.02:
                warnings.append(f"Latência alta: {m} p95={st['p95']*1000:.2f} ms (>=20ms).")
        else:
            missing.append(m)

    # Interpretação combinada (boa para priorizar camada C)
    cpu_ok = cpu_col in df.columns if cpu_col else False
    if cpu_ok and "DBLoadRelativeToNumVCPUs" in df.columns:
        cpu_p95 = _series_stats(df[cpu_col])["p95"]
        load_p95 = _series_stats(df["DBLoadRelativeToNumVCPUs"])["p95"]
        if cpu_p95 is not None and load_p95 is not None:
            if cpu_p95 >= 70 and load_p95 >= 1.0:
                warnings.append("Sinal forte de saturação: CPU p95 alta e DBLoad/vCPU p95 >= 1 (fila recorrente). Priorize Camada C (Top SQL) e Camada B (índices/fragmentação).")
            elif cpu_p95 < 60 and load_p95 >= 1.0:
                warnings.append("DBLoad/vCPU alto com CPU não tão alta sugere waits (I/O, lock, etc). Priorize Camada C (waits/locks/top SQL) e correlacione com latências.")

    # remove duplicados preservando ordem
    def _uniq(seq):
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                out.append(x)
                seen.add(x)
        return out

    return _uniq(warnings), _uniq(missing)


# ============================================================
# Localização dos arquivos (repo db_health)
# ============================================================

def _find_layer0_collected(run_yaml: Path, week: str) -> Optional[Path]:
    base = run_yaml.parent / "layer0" / "collected" / week
    candidates = [
        base / "layer0_collected.csv",
        base / "layer0_inventory_normalized.csv",
        base / "layer0_inventory.normalized.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _resolve_cloudwatch_wide_path(run_yaml: Path, cfg: dict, week: str) -> Path:
    wide_path = Path(_strip(cfg.get("layerA", {}).get("cloudwatch_wide", "")))
    if wide_path and wide_path.exists():
        return wide_path
    return run_yaml.parent / "layerA" / "collected" / week / "cloudwatch_wide.csv"


# ============================================================
# Eventos correlacionados (Top-N)
# ============================================================

def _top_events(df: pd.DataFrame, metric: str, how: str = "max", top_n: int = 5) -> pd.DataFrame:
    if metric not in df.columns:
        return pd.DataFrame()
    tmp = df[["ts", metric]].copy()
    tmp[metric] = pd.to_numeric(tmp[metric], errors="coerce")
    tmp = tmp.dropna()
    if tmp.empty:
        return pd.DataFrame()
    tmp = tmp.sort_values(metric, ascending=(how != "max")).head(top_n)
    return tmp


def _event_snapshot_table(
    df: pd.DataFrame,
    events: pd.DataFrame,
    event_name: str,
    show_cols: List[str],
    window_minutes: int
) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame()

    rows = []
    for _, r in events.iterrows():
        when = r["ts"]  # tz-aware (vem do df)
        snap = _nearest_row(df, when, window_minutes=window_minutes)
        if snap is None:
            continue

        row = {
            "quando": pd.Timestamp(when).strftime("%Y-%m-%d %H:%M"),
            "evento": event_name,
        }

        for c in show_cols:
            if c not in df.columns:
                row[c] = "N/A"
                continue

            v = snap.get(c, None)

            if c in ("CPUUtilization", "MemFreePct", "StorageFreePct", "StorageUsedPct"):
                row[c] = _fmt_num(v, "%")
            elif c in ("ReadLatency", "WriteLatency"):
                vv = None
                try:
                    vv = float(v) * 1000.0
                except Exception:
                    vv = None
                row[c] = _fmt_num(vv, "ms")
            elif c in ("TotalIOPS", "ReadIOPS", "WriteIOPS"):
                row[c] = _fmt_num(v, "IOPS")
            elif c in ("FreeableMemory",):
                vv = None
                try:
                    vv = float(v) / (1024**3)
                except Exception:
                    vv = None
                row[c] = _fmt_num(vv, "GiB")
            elif c in ("StorageFreeGiB", "StorageUsedGiB", "MemFreeGiB"):
                row[c] = _fmt_num(v, "GiB")
            else:
                row[c] = _fmt_num(v)

        rows.append(row)

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ============================================================
# Engine detection
# ============================================================

def _engine_mode(cfg: dict, l0: dict) -> Tuple[bool, bool, bool]:
    """
    Retorna (is_rds, is_aurora, is_serverless_v2)
    Prioriza cfg.engine, depois l0.deployment_type.
    """
    eng = _strip(cfg.get("engine", "")).lower()
    dep = _strip(l0.get("deployment_type", "")).lower()

    # cfg.engine (preferencial)
    if eng:
        if "aurora" in eng and "serverless" in eng:
            return (False, True, True)
        if "aurora" in eng:
            return (False, True, False)
        if "community" in eng or "mysql_community" in eng:
            return (True, False, False)

    # fallback: l0.deployment_type
    if dep:
        if dep.startswith("aurora_") and "serverless" in dep:
            return (False, True, True)
        if dep.startswith("aurora_"):
            return (False, True, False)
        if "rds" in dep or "community" in dep:
            return (True, False, False)

    # desconhecido -> assume “não RDS” para não exigir storage/iops
    return (False, False, False)


# ============================================================
# MAIN
# ============================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True, help="Caminho do run.yaml da instância")
    ap.add_argument("--out", default="", help="Arquivo HTML de saída (opcional)")
    ap.add_argument("--event-window-minutes", type=int, default=5, help="Janela para correlacionar eventos (± minutos)")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip(cfg.get("instance")) or run_yaml.parent.name
    week = cfg.get("week")
    if isinstance(week, dt.date):
        week = week.isoformat()
    week = _strip(week)
    if not week:
        raise SystemExit("run.yaml precisa ter 'week: YYYY-MM-DD'.")

    # Layer0 collected
    l0 = {}
    l0_path = _find_layer0_collected(run_yaml, week)
    if l0_path and l0_path.exists():
        l0 = _read_single_row_csv(l0_path)

    is_rds, is_aurora, is_serverless = _engine_mode(cfg, l0)

    # CloudWatch wide
    wide_path = _resolve_cloudwatch_wide_path(run_yaml, cfg, week)
    if not wide_path.exists():
        raise SystemExit(f"Não encontrei cloudwatch_wide.csv: {wide_path}")

    df = pd.read_csv(wide_path)
    df = _parse_ts(df)

    # TotalIOPS derivado (se tiver Read/Write)
    if "TotalIOPS" not in df.columns and "ReadIOPS" in df.columns and "WriteIOPS" in df.columns:
        df["TotalIOPS"] = pd.to_numeric(df["ReadIOPS"], errors="coerce").fillna(0) + pd.to_numeric(df["WriteIOPS"], errors="coerce").fillna(0)

    # MemFreePct derivado (se tiver FreeableMemory + ram_gib)
    ram_gib = _safe_float(l0.get("ram_gib"))
    if "FreeableMemory" in df.columns and ram_gib:
        mem_total_b = ram_gib * (1024**3)
        free_b = pd.to_numeric(df["FreeableMemory"], errors="coerce")
        df["MemFreeGiB"] = _b_to_gib(free_b)
        df["MemFreePct"] = (free_b / mem_total_b) * 100.0

    # Storage derivado (só RDS)
    allocated_gib = _safe_float(l0.get("allocated_storage_gib"))
    free_storage_col = _pick_col(df, "FreeStorageSpace_Minimum")
    if is_rds and free_storage_col and allocated_gib:
        alloc_bytes = allocated_gib * (1024**3)
        free_b = pd.to_numeric(df[free_storage_col], errors="coerce")
        used_b = (alloc_bytes - free_b).clip(lower=0)
        df["StorageFreeGiB"] = _b_to_gib(free_b)
        df["StorageUsedGiB"] = _b_to_gib(used_b)
        df["StorageFreePct"] = (free_b / alloc_bytes) * 100.0
        df["StorageUsedPct"] = 100.0 - df["StorageFreePct"]

    # =========================
    # KPIs
    # =========================
    kpis = []

    cpu_col = _pick_col(df, "CPUUtilization")
    if cpu_col:
        kpis.append(_kpi_row("CPUUtilization", df[cpu_col], unit="%"))

    if "DBLoadRelativeToNumVCPUs" in df.columns:
        kpis.append(_kpi_row("DBLoadRelativeToNumVCPUs", df["DBLoadRelativeToNumVCPUs"], unit="AAS/vCPU"))

    if "DatabaseConnections" in df.columns:
        kpis.append(_kpi_row("DatabaseConnections", df["DatabaseConnections"], unit="conexões"))

    if "FreeableMemory" in df.columns:
        kpis.append(_kpi_row("FreeableMemory", df["FreeableMemory"], unit="GiB",
                             transform=lambda s: _b_to_gib(pd.to_numeric(s, errors="coerce"))))

    if "MemFreePct" in df.columns:
        kpis.append(_kpi_row("MemFreePct", df["MemFreePct"], unit="%"))

    if is_rds:
        if "StorageFreeGiB" in df.columns:
            kpis.append(_kpi_row("StorageFreeGiB", df["StorageFreeGiB"], unit="GiB"))
        if "StorageFreePct" in df.columns:
            kpis.append(_kpi_row("StorageFreePct", df["StorageFreePct"], unit="%"))
        if "StorageUsedPct" in df.columns:
            kpis.append(_kpi_row("StorageUsedPct", df["StorageUsedPct"], unit="%"))
        if "TotalIOPS" in df.columns:
            kpis.append(_kpi_row("TotalIOPS", df["TotalIOPS"], unit="IOPS"))

    for m in ("ReadLatency", "WriteLatency"):
        if m in df.columns:
            kpis.append(_kpi_row(m, df[m], unit="ms",
                                 transform=lambda s: _sec_to_ms(pd.to_numeric(s, errors="coerce"))))

    if is_serverless:
        for m in AURORA_SERVERLESS_EXTRA:
            if m in df.columns:
                kpis.append(_kpi_row(m, df[m], unit=""))

    kpi_df = pd.DataFrame(kpis)
    if not kpi_df.empty:
        kpi_df["min"] = kpi_df.apply(lambda r: _fmt_num(r["min"], r["unidade"]), axis=1)
        kpi_df["avg"] = kpi_df.apply(lambda r: _fmt_num(r["avg"], r["unidade"]), axis=1)
        kpi_df["p95"] = kpi_df.apply(lambda r: _fmt_num(r["p95"], r["unidade"]), axis=1)
        kpi_df["max"] = kpi_df.apply(lambda r: _fmt_num(r["max"], r["unidade"]), axis=1)
        kpi_df = kpi_df[["métrica", "min", "avg", "p95", "max"]]

    # =========================
    # Heurísticas + Cobertura
    # =========================
    warnings, missing = _heuristics(df, l0, is_rds=is_rds, is_aurora=is_aurora)

    expected = list(BASE_METRICS)
    if is_rds:
        expected += RDS_ONLY_METRICS
    if is_serverless:
        expected += AURORA_SERVERLESS_EXTRA

    # cobertura por engine (com alias)
    for m in expected:
        if m == "FreeStorageSpace_Minimum":
            if is_rds and not _pick_col(df, "FreeStorageSpace_Minimum"):
                if m not in missing:
                    missing.append(m)
        else:
            if m not in df.columns:
                # CPUUtilization pode estar em alias
                if m == "CPUUtilization":
                    if not _pick_col(df, "CPUUtilization"):
                        if m not in missing:
                            missing.append(m)
                else:
                    if m not in missing:
                        missing.append(m)

    warnings_html = "<br/>".join([_html_escape(f"• {w}") for w in warnings]) if warnings else _html_escape(
        "• Nenhuma anomalia óbvia detectada pelas heurísticas desta camada (confirmar com Camada B/C)."
    )
    missing_html = "<br/>".join([_html_escape(f"• {m}") for m in missing]) if missing else ""

    # =========================
    # Eventos correlacionados (Top 5)
    # =========================
    event_cols = [
        "CPUUtilization",
        "DBLoadRelativeToNumVCPUs",
        "DatabaseConnections",
        "TotalIOPS",
        "ReadLatency",
        "WriteLatency",
        "MemFreePct",
    ]
    if is_rds:
        event_cols += ["StorageFreePct"]

    # Ajusta CPU na tabela de eventos para usar a coluna real (alias)
    # (a tabela usa colunas do df; então, se CPU está em alias, renomeia temporariamente)
    df_events = df.copy()
    cpu_real = _pick_col(df_events, "CPUUtilization")
    if cpu_real and cpu_real != "CPUUtilization":
        df_events["CPUUtilization"] = df_events[cpu_real]

    event_blocks = []

    ev_cpu = _top_events(df_events, "CPUUtilization", how="max", top_n=5)
    t_cpu = _event_snapshot_table(df_events, ev_cpu, "Top 5 picos de CPU", show_cols=event_cols, window_minutes=args.event_window_minutes)
    if not t_cpu.empty:
        event_blocks.append(_df_to_html_table(t_cpu, f"Eventos correlacionados (janela ±{args.event_window_minutes}min) — Top 5 picos de CPU"))

    ev_load = _top_events(df_events, "DBLoadRelativeToNumVCPUs", how="max", top_n=5)
    t_load = _event_snapshot_table(df_events, ev_load, "Top 5 picos de DBLoad/vCPU", show_cols=event_cols, window_minutes=args.event_window_minutes)
    if not t_load.empty:
        event_blocks.append(_df_to_html_table(t_load, f"Eventos correlacionados (janela ±{args.event_window_minutes}min) — Top 5 picos de DBLoad/vCPU"))

    events_html = "".join(event_blocks) if event_blocks else ""

    # =========================
    # Plots (somente relevantes)
    # =========================
    plot_specs = [
        ("CPUUtilization", "CPU Utilization", "CPU (%)", None),
        ("DBLoadRelativeToNumVCPUs", "DB Load / vCPU", "AAS/vCPU", None),
        ("DatabaseConnections", "Database Connections", "conexões", None),
        ("ReadLatency", "Read Latency", "ms", lambda s: _sec_to_ms(pd.to_numeric(s, errors="coerce"))),
        ("WriteLatency", "Write Latency", "ms", lambda s: _sec_to_ms(pd.to_numeric(s, errors="coerce"))),
    ]

    if "MemFreePct" in df.columns:
        plot_specs.append(("MemFreePct", "Memória livre (%)", "%", None))
    elif "FreeableMemory" in df.columns:
        plot_specs.append(("FreeableMemory", "Freeable Memory", "GiB", lambda s: _b_to_gib(pd.to_numeric(s, errors="coerce"))))

    if is_rds:
        if "StorageFreePct" in df.columns:
            plot_specs.append(("StorageFreePct", "Storage livre (%)", "%", None))
        if "StorageUsedPct" in df.columns:
            plot_specs.append(("StorageUsedPct", "Storage usado (%)", "%", None))
        if "TotalIOPS" in df.columns:
            plot_specs.append(("TotalIOPS", "Total IOPS (derivado)", "IOPS", None))

    if is_serverless:
        for m in AURORA_SERVERLESS_EXTRA:
            if m in df.columns:
                plot_specs.append((m, m, "", None))

    plots_cards = []
    for col, title_plot, ylabel, transform in plot_specs:
        if col not in df_events.columns:
            continue
        png_bytes = _save_plot_png_bytes(
            df_events, col, title_plot, ylabel,
            transform=(lambda s, t=transform: t(s)) if callable(transform) else None
        )
        data_uri = _png_bytes_to_data_uri(png_bytes)
        plots_cards.append(f"""
        <div class="card">
          <div class="card-title">{_html_escape(title_plot)}</div>
          <img src="{data_uri}" alt="{_html_escape(title_plot)}"/>
        </div>
        """)

    plots_html = (
        "<div class='grid'>" + "".join(plots_cards) + "</div>"
        if plots_cards else
        "<div class='card'><div class='card-title'>Gráficos</div><div class='note'>Sem métricas plotáveis encontradas no cloudwatch_wide.csv.</div></div>"
    )

    # =========================
    # Cabeçalho / contexto
    # =========================
    now_local = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "Camada A (CloudWatch / Vital Signs)"

    engine_desc = "Aurora" if is_aurora else ("RDS MySQL Community" if is_rds else "Desconhecido")
    subtitle = f"Instância: {instance} · Semana: {week} · Engine: {engine_desc} · Gerado em: {now_local}"

    interpret = []
    vcpu = _safe_float(l0.get("vcpu"))
    provisioned_iops = _safe_float(l0.get("provisioned_iops"))

    if vcpu:
        interpret.append("Regra prática: DBLoad/vCPU > 1 indica fila/saturação recorrente.")
        interpret.append(f"vCPU (Camada 0): {int(vcpu)}.")

    if ram_gib:
        interpret.append(f"RAM (Camada 0): {int(ram_gib)} GiB. MemFreePct é calculado a partir de FreeableMemory.")
    else:
        if is_rds:
            interpret.append("RAM (Camada 0) ausente: MemFreePct não será calculado (RDS).")
        # (Aurora: não precisa falar isso — evita ruído)

    if is_rds and allocated_gib:
        interpret.append(f"Storage alocado (Camada 0): {int(allocated_gib)} GiB. StorageFreePct usa FreeStorageSpace_Minimum.")

    if is_rds and provisioned_iops:
        interpret.append(f"Provisioned IOPS (Camada 0): {int(provisioned_iops)}. TotalIOPS p95 alto (~>=80%) sugere pressão de I/O.")

    interpret.append("Dica prática: combine picos de CPU + DBLoad/vCPU + latências para separar gargalo de CPU vs gargalo de waits (I/O/lock).")
    interpret.append("Quando houver suspeita, a causa-raiz costuma aparecer na Camada C (Top SQL / waits / locks) e Camada B (índices, fragmentação, volumetria).")

    interpret_html = "<br/>".join([_html_escape(x) for x in interpret])

    # =========================
    # Output HTML
    # =========================
    html_dir = run_yaml.parent / "reports" / "html" / week
    html_dir.mkdir(parents=True, exist_ok=True)
    out_html = Path(args.out) if args.out else (html_dir / "layerA_cloudwatch.html")

    css = """
    <style>
      :root { --bg:#0b0f14; --panel:#111826; --panel2:#0f1622; --text:#e6eefc; --muted:#9fb1cc; --border:#223048; --warn:#f4d03f; }
      body { margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial; background:var(--bg); color:var(--text); }
      .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
      h1 { font-size: 22px; margin: 0 0 6px 0; }
      .sub { color:var(--muted); font-size: 13px; margin-bottom: 14px; }
      .grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:14px; }
      .card { background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 1px solid var(--border); border-radius: 16px; padding: 14px; }
      .card-title { font-weight: 700; margin-bottom: 10px; color: #d7e5ff; }
      .note { color: var(--muted); font-size: 12px; line-height: 1.45; }
      .warn { border: 1px solid rgba(244,208,63,.35); box-shadow: 0 0 0 1px rgba(244,208,63,.12) inset; }
      .warn .card-title { color: var(--warn); }
      img { width: 100%; height: auto; border-radius: 12px; border: 1px solid rgba(255,255,255,.08); }
      .tablewrap { overflow:auto; }
      table.tbl { width:100%; border-collapse: collapse; font-size: 13px; }
      table.tbl th, table.tbl td { padding: 8px 8px; border-bottom: 1px solid rgba(255,255,255,.06); text-align: left; }
      table.tbl th { color: var(--muted); font-weight: 600; }
      @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
    </style>
    """

    kpi_html = _df_to_html_table(kpi_df, "KPIs (min / avg / p95 / max)") if not kpi_df.empty else ""

    missing_block = ""
    if missing_html:
        missing_block = f"""
        <div class="card">
          <div class="card-title">Cobertura (o que NÃO veio exportado)</div>
          <div class="note">{missing_html}</div>
        </div>
        """

    html = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{_html_escape(title)} - {_html_escape(instance)}</title>
  {css}
</head>
<body>
  <div class="wrap">
    <h1>{_html_escape(title)}</h1>
    <div class="sub">{_html_escape(subtitle)}</div>

    <div class="card">
      <div class="card-title">Como ler esta camada</div>
      <div class="note">{interpret_html}</div>
    </div>

    <div class="card warn" style="margin-top:14px;">
      <div class="card-title">Pré-análises automáticas (heurísticas)</div>
      <div class="note">{warnings_html}</div>
    </div>

    <div style="margin-top:14px;">
      {missing_block}
    </div>

    <div style="margin-top:14px;">
      {kpi_html}
    </div>

    <div style="margin-top:14px;">
      {events_html}
    </div>

    <div style="margin-top:14px;">
      {plots_html}
    </div>

    <div class="note" style="margin-top:18px;">
      <b>Nota:</b> esta camada mede “sintomas” (CloudWatch). A causa-raiz normalmente aparece na Camada B (índices/volumetria) e Camada C (top SQL / waits / locks).
    </div>

  </div>
</body>
</html>
"""
    out_html.write_text(html, encoding="utf-8")
    print(f"[OK] report gerado (HTML com imagens embutidas): {out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
