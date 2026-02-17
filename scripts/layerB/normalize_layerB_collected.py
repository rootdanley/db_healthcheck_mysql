#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import datetime as dt
from pathlib import Path

import pandas as pd
import yaml


# TSV esperados na camada B
# (aceita também top_indexes_tables.tsv como alias de top_indexes_by_tables.tsv)
SCHEMAS = {
    "instance_facts.tsv": [
        "collected_at",
        "hostname",
        "server_uuid",
        "server_id",
        "version",
        "version_comment",
        "read_only",
        "super_read_only",
        "max_connections",
        "innodb_buffer_pool_size_bytes",
        "innodb_log_file_size_bytes",
        "innodb_flush_log_at_trx_commit",
        "sync_binlog",
    ],
    "db_sizes.tsv": ["db", "size_mb", "size_gb"],
    "top_tables.tsv": ["db", "table_name", "engine", "table_rows", "total_mb", "data_mb", "index_mb"],
    "top_indexes.tsv": ["db", "table_name", "index_name", "pages", "approx_index_mb"],
    "table_fragmentation.tsv": ["db", "table_name", "engine", "data_free_mb", "total_mb", "free_pct"],
    "unused_indexes.tsv": ["db", "table_name", "index_name", "approx_index_mb"],
    "table_scans.tsv": ["db", "table_name", "rows_full_scanned", "latency"],
    "top_indexes_by_tables.tsv": ["db", "table_name", "index_count", "index_names"],
}

ALIASES = {
    "top_indexes_tables.tsv": "top_indexes_by_tables.tsv",
}


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


def _default_in_dir(run_yaml: Path, week: str) -> Path:
    return run_yaml.parent / "layerB" / "inputs" / week


def _default_out_dir(run_yaml: Path, week: str) -> Path:
    return run_yaml.parent / "layerB" / "collected" / week


def _read_tsv_no_header(path: Path) -> pd.DataFrame:
    if (not path.exists()) or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(
            path,
            sep="\t",
            header=None,
            dtype=str,
            engine="python",
            quoting=csv.QUOTE_NONE,
            keep_default_na=False,
            on_bad_lines="skip",
        )
    except Exception:
        return pd.DataFrame()

    for c in df.columns:
        df[c] = df[c].map(_strip)

    # remove linhas totalmente vazias
    if not df.empty:
        df = df.dropna(how="all")
        df = df[(df.astype(str).apply(lambda r: any(_strip(v) for v in r), axis=1))]

    return df


def _apply_schema_flexible(df: pd.DataFrame, canonical_name: str) -> pd.DataFrame:
    """
    - Se bater colunas: renomeia
    - Se vier mais colunas: renomeia as primeiras e o resto vira extra_*
    - Se vier menos: cria colunas faltantes vazias
    """
    cols = SCHEMAS.get(canonical_name)
    if cols is None:
        # desconhecido -> col1..colN
        if df.empty:
            return pd.DataFrame()
        out = df.copy()
        out.columns = [f"col{i+1}" for i in range(out.shape[1])]
        return out

    if df.empty:
        return pd.DataFrame(columns=cols)

    out = df.copy()
    n = out.shape[1]
    k = len(cols)

    if n == k:
        out.columns = cols
        return out

    if n > k:
        new_cols = cols + [f"extra_{i+1}" for i in range(n - k)]
        out.columns = new_cols
        return out

    # n < k
    out.columns = cols[:n]
    for miss in cols[n:]:
        out[miss] = ""
    return out[cols]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True)
    ap.add_argument("--week", default="")
    ap.add_argument("--in-dir", default="")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip(cfg.get("instance")) or run_yaml.parent.name
    week = _strip(args.week) or _strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter week: YYYY-MM-DD (ou passe --week).")

    in_dir = Path(_expand_vars(_strip(args.in_dir), instance=instance, week=week)) if _strip(args.in_dir) else _default_in_dir(run_yaml, week)
    out_dir = Path(_expand_vars(_strip(args.out_dir), instance=instance, week=week)) if _strip(args.out_dir) else _default_out_dir(run_yaml, week)

    if not in_dir.exists():
        raise SystemExit(f"Diretório de inputs não encontrado: {in_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)

    tsv_files = sorted([p for p in in_dir.glob("*.tsv") if p.is_file()])
    if args.verbose:
        print(f"[DEBUG] in_dir={in_dir} tsv_files={len(tsv_files)}")

    # index por nome -> Path
    by_name = {p.name: p for p in tsv_files}

    manifest = []

    # 1) Normaliza todos os "esperados"
    for expected in SCHEMAS.keys():
        # alias: se existir alias no input, usa alias
        src_name = expected
        # se o esperado não existe mas um alias existe, usa alias
        for alias, canon in ALIASES.items():
            if canon == expected and alias in by_name and expected not in by_name:
                src_name = alias

        src_path = by_name.get(src_name)
        if src_path and src_path.exists() and src_path.stat().st_size > 0:
            df_raw = _read_tsv_no_header(src_path)
            df_norm = _apply_schema_flexible(df_raw, expected)
            note = ""
            source_size = int(src_path.stat().st_size)
        else:
            df_norm = pd.DataFrame(columns=SCHEMAS[expected])
            note = "placeholder: arquivo .tsv não encontrado/vazio neste week"
            source_size = 0

        out_csv = out_dir / (Path(expected).stem + ".csv")
        df_norm.to_csv(out_csv, index=False)

        manifest.append({
            "dataset": expected,
            "source_file": src_name if src_name in by_name else "",
            "out_csv": str(out_csv),
            "rows": int(df_norm.shape[0]),
            "cols": int(df_norm.shape[1]),
            "source_size_bytes": source_size,
            "note": note,
        })

        if args.verbose:
            print(f"[DEBUG] {expected} <- {src_name} rows={df_norm.shape[0]} cols={df_norm.shape[1]}")

    # 2) Normaliza quaisquer TSV extras não previstos (para não “sumir”)
    expected_and_alias = set(SCHEMAS.keys()) | set(ALIASES.keys())
    extras = [p for p in tsv_files if p.name not in expected_and_alias]
    for p in extras:
        df_raw = _read_tsv_no_header(p)
        if df_raw.empty:
            continue
        df_raw.columns = [f"col{i+1}" for i in range(df_raw.shape[1])]
        out_csv = out_dir / (p.stem + ".csv")
        df_raw.to_csv(out_csv, index=False)
        manifest.append({
            "dataset": p.name,
            "source_file": p.name,
            "out_csv": str(out_csv),
            "rows": int(df_raw.shape[0]),
            "cols": int(df_raw.shape[1]),
            "source_size_bytes": int(p.stat().st_size),
            "note": "extra (não mapeado em schema)",
        })

    man_df = pd.DataFrame(manifest)
    man_path = out_dir / "layerB_manifest.csv"
    man_df.to_csv(man_path, index=False)

    print(f"[OK] inputs:    {in_dir}")
    print(f"[OK] collected: {out_dir}")
    print(f"[OK] manifest:  {man_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
