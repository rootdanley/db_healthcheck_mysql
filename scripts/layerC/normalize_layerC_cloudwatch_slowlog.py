#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
import yaml


# ------------------------------------------------------------
# Datasets canônicos (nome do arquivo => dataset)
# ------------------------------------------------------------
DATASET_BY_STEM = {
    # statements
    "top_sql_total_time": "top_sql_total_time",
    "top_sql_calls": "top_sql_calls",
    "top_sql_lock_total": "top_sql_lock_total",
    "top_sql_worst_single": "top_sql_worst_single",
    "top_sql_rows_examined": "top_sql_rows_examined",

    # atores
    "top_users_total_time": "top_users_total_time",
    "top_srcip_total_time": "top_srcip_total_time",
}

# aliases tolerados (caso você salve com nome diferente)
DATASET_ALIASES = {
    "top_sql_lock_total_time": "top_sql_lock_total",
    "top_sql_lock": "top_sql_lock_total",
    "top_sql_worst": "top_sql_worst_single",
    "top_ips_total_time": "top_srcip_total_time",
    "top_hosts_total_time": "top_srcip_total_time",
}


# colunas numéricas que aparecem nos exports
NUMERIC_COLS = {
    "calls",
    "total_query_time_s",
    "avg_query_time_s",
    "worst_query_time_s",
    "total_time_s",
    "avg_time_s",
    "p95_time_s",
    "worst_time_s",
    "total_lock_time_s",
    "avg_lock_time_s",
    "worst_lock_time_s",
    "total_rows_examined",
    "avg_rows_examined",
    "worst_rows_examined",
    "rows_sent",
    "rows_examined",
    "query_time_s",
    "lock_time_s",
}

# candidatos de coluna de statement / user / ip
STMT_COLS = ["statement", "stmt", "hc_stmt", "hc_stmt_txt"]
USER_COLS = ["db_user", "user", "hc_user"]
IP_COLS = ["src_ip", "srcip", "hc_srcip", "client_ip"]


def _strip(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return str(x).strip()


def _expand_vars(s: str, *, instance: str, week: str) -> str:
    if not s:
        return s
    return (
        s.replace("{week}", week).replace("${week}", week).replace("$week", week)
         .replace("{instance}", instance).replace("${instance}", instance).replace("$instance", instance)
    )


def _resolve_in_dir(run_yaml: Path, cfg: dict, instance: str, week: str) -> Path:
    raw = _strip((cfg.get("layerC") or {}).get("dir"))
    if raw:
        raw = _expand_vars(raw, instance=instance, week=week)
        return Path(raw)
    return run_yaml.parent / "layerC" / "inputs" / week


def _resolve_out_dir(run_yaml: Path, instance: str, week: str) -> Path:
    return run_yaml.parent / "layerC" / "collected" / week


def _read_any_csv(path: Path) -> pd.DataFrame:
    if (not path.exists()) or path.stat().st_size == 0:
        return pd.DataFrame()

    # tenta CSV padrão (vírgula). Se der “1 coluna só”, tenta TSV.
    try:
        df = pd.read_csv(path, dtype=str)
        if df.shape[1] <= 1:
            raise ValueError("provável TSV")
        return df
    except Exception:
        try:
            return pd.read_csv(path, sep="\t", dtype=str)
        except Exception:
            return pd.DataFrame()


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [c.strip().lstrip("\ufeff") for c in out.columns]

    # drop colunas do CloudWatch tipo @ptr, @timestamp, etc.
    drop = [c for c in out.columns if c.startswith("@")]
    if drop:
        out = out.drop(columns=drop, errors="ignore")

    # strip valores
    for c in out.columns:
        out[c] = out[c].map(_strip)

    return out


def _pick_first(df: pd.DataFrame, candidates: list[str]) -> str | None:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _to_numeric_safe(s: pd.Series) -> pd.Series:
    x = s.astype(str)
    x = x.str.replace("\xa0", " ", regex=False).str.replace(" ", "", regex=False)
    x = x.replace({"": None, "nan": None, "NaN": None, "N/A": None})
    return pd.to_numeric(x, errors="coerce")


def _dataset_from_filename(p: Path) -> str | None:
    stem = p.stem.strip().lower()
    if stem in DATASET_BY_STEM:
        return DATASET_BY_STEM[stem]
    if stem in DATASET_ALIASES:
        return DATASET_ALIASES[stem]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True)
    ap.add_argument("--week", default="", help="override week (YYYY-MM-DD)")
    ap.add_argument("--in-dir", default="", help="override inputs dir")
    ap.add_argument("--out-dir", default="", help="override output dir")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip(cfg.get("instance")) or run_yaml.parent.name
    week = _strip(args.week) or _strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter week: YYYY-MM-DD (ou passe --week).")

    in_dir = Path(_expand_vars(_strip(args.in_dir), instance=instance, week=week)) if _strip(args.in_dir) else _resolve_in_dir(run_yaml, cfg, instance, week)
    out_dir = Path(_expand_vars(_strip(args.out_dir), instance=instance, week=week)) if _strip(args.out_dir) else _resolve_out_dir(run_yaml, instance, week)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not in_dir.exists():
        raise SystemExit(f"Diretório de inputs não encontrado: {in_dir}")

    files = sorted([p for p in in_dir.glob("*.csv") if p.is_file()])
    if not files:
        raise SystemExit(f"Nenhum CSV encontrado em: {in_dir}")

    manifest = []

    for f in files:
        ds = _dataset_from_filename(f)  # ✅ primeiro pelo nome do arquivo
        df0 = _norm_cols(_read_any_csv(f))

        if df0.empty:
            manifest.append({"source_file": f.name, "dataset": "", "rows": 0, "note": "arquivo vazio ou falha de leitura"})
            continue

        if not ds:
            # fallback: se o nome não bate, tenta inferir por colunas mínimas
            cols = set(df0.columns)
            if {"stmt", "calls", "total_time_s"}.issubset(cols) or {"statement", "calls", "total_time_s"}.issubset(cols):
                ds = "top_sql_calls"
            elif {"stmt", "calls", "total_query_time_s"}.issubset(cols) or {"statement", "calls", "total_query_time_s"}.issubset(cols):
                ds = "top_sql_total_time"
            elif {"stmt", "calls", "total_lock_time_s"}.issubset(cols) or {"statement", "calls", "total_lock_time_s"}.issubset(cols):
                ds = "top_sql_lock_total"
            elif {"stmt", "calls", "worst_query_time_s"}.issubset(cols) or {"statement", "calls", "worst_query_time_s"}.issubset(cols):
                ds = "top_sql_worst_single"
            elif {"stmt", "calls", "total_rows_examined"}.issubset(cols) or {"statement", "calls", "total_rows_examined"}.issubset(cols):
                ds = "top_sql_rows_examined"
            elif {"hc_user", "total_query_time_s"}.issubset(cols) or {"user", "total_query_time_s"}.issubset(cols):
                ds = "top_users_total_time"
            elif {"hc_srcip", "total_query_time_s"}.issubset(cols) or {"srcip", "total_query_time_s"}.issubset(cols):
                ds = "top_srcip_total_time"
            else:
                manifest.append({"source_file": f.name, "dataset": "", "rows": int(df0.shape[0]), "note": "dataset não reconhecido"})
                continue

        out = df0.copy()

        # normaliza statement/user/ip
        stmt_col = _pick_first(out, STMT_COLS)
        if stmt_col and stmt_col != "statement":
            out = out.rename(columns={stmt_col: "statement"})
        if "statement" not in out.columns:
            out["statement"] = ""

        user_col = _pick_first(out, USER_COLS)
        if user_col and user_col != "db_user":
            out = out.rename(columns={user_col: "db_user"})

        ip_col = _pick_first(out, IP_COLS)
        if ip_col and ip_col != "src_ip":
            out = out.rename(columns={ip_col: "src_ip"})

        # converte numéricos conhecidos
        for c in out.columns:
            if c in NUMERIC_COLS:
                out[c] = _to_numeric_safe(out[c])

        # metadata
        out.insert(0, "instance", instance)
        out.insert(1, "week", week)
        out.insert(2, "dataset", ds)
        out["source_file"] = f.name

        # escreve 1 arquivo por dataset em collected/<week>
        dst = out_dir / f"{ds}.csv"
        if dst.exists() and dst.stat().st_size > 0:
            prev = pd.read_csv(dst, dtype=str)
            merged = pd.concat([prev, out.astype(str)], ignore_index=True)
            merged.to_csv(dst, index=False)
        else:
            out.to_csv(dst, index=False)

        manifest.append({"source_file": f.name, "dataset": ds, "rows": int(out.shape[0]), "note": ""})

        if args.verbose:
            print(f"[DEBUG] {f.name} -> {ds} ({out.shape[0]} linhas)")

    man_df = pd.DataFrame(manifest)
    man_path = out_dir / "layerC_manifest.csv"
    man_df.to_csv(man_path, index=False)

    print(f"[OK] inputs:   {in_dir}")
    print(f"[OK] outputs:  {out_dir}")
    print(f"[OK] manifest: {man_path}")


if __name__ == "__main__":
    main()
