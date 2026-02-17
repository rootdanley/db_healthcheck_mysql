#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Camada C — Report (HTML)
Lê CSVs normalizados em: instances/<inst>/layerC/collected/<week>/
Gera HTML em:           instances/<inst>/reports/html/<week>/

Datasets esperados (arquivos .csv, nome livre, mas colunas devem bater):
  - top_sql_total_time
  - top_sql_calls
  - top_sql_lock_total
  - top_sql_worst_single
  - top_sql_rows_examined
  - top_users_total_time
  - top_srcip_total_time

Melhorias:
- _statement_full NÃO aparece como coluna (coluna interna, prefixo "_").
- statement aparece com preview + "expandir" com SQL completo (sem deformar).
- Robusto contra NaN/float em statement.
"""

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
import yaml


# =========================
# Config
# =========================
STMT_PREVIEW_CHARS_DEFAULT = 300  # preview (o SQL completo fica no expandir)

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
}

DATASET_SPECS = [
    ("top_sql_total_time", "TOP SQL — Resource Intensive (tempo total)", [
        "statement", "calls", "total_query_time_s", "avg_query_time_s", "worst_query_time_s"
    ]),
    ("top_sql_calls", "TOP SQL — Calls (mais executadas no slow log)", [
        "statement", "calls", "total_time_s", "avg_time_s", "p95_time_s", "worst_time_s", "avg_rows_examined"
    ]),
    ("top_sql_lock_total", "TOP SQL — Lock (lock_time total)", [
        "statement", "calls", "total_lock_time_s", "avg_lock_time_s", "worst_lock_time_s"
    ]),
    ("top_sql_worst_single", "TOP SQL — “Pior” (maior Query_time individual)", [
        "statement", "worst_query_time_s", "calls", "total_query_time_s"
    ]),
    ("top_sql_rows_examined", "TOP SQL — Rows_examined (total)", [
        "statement", "calls", "total_rows_examined", "worst_rows_examined", "avg_rows_examined", "total_query_time_s"
    ]),
    ("top_users_total_time", "Top 5 usuários por tempo total de query", [
        "db_user", "calls", "total_query_time_s", "avg_query_time_s", "worst_query_time_s"
    ]),
    ("top_srcip_total_time", "Top 5 IPs de origem por tempo total de query", [
        "src_ip", "calls", "total_query_time_s", "avg_query_time_s", "worst_query_time_s"
    ]),
]

# aliases para normalizar colunas
ALIASES = {
    "statement": ["statement", "stmt", "hc_stmt", "hc_stmt_txt"],
    "db_user": ["db_user", "user", "hc_user"],
    "src_ip": ["src_ip", "srcip", "hc_srcip", "src_ip_addr"],
}


# =========================
# Utils
# =========================
def _strip(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return str(x).strip()


def _html_escape(s: str) -> str:
    s = _strip(s)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def _read_csv_any(path: Path) -> pd.DataFrame:
    if (not path.exists()) or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype=object)
        if df.shape[1] <= 1:
            raise ValueError("provável TSV")
        return df
    except Exception:
        try:
            return pd.read_csv(path, sep="\t", dtype=object)
        except Exception:
            return pd.DataFrame()


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip().lstrip("\ufeff") for c in out.columns]
    drop = [c for c in out.columns if str(c).startswith("@")]
    if drop:
        out = out.drop(columns=drop, errors="ignore")
    return out


def _pick_first_col(df: pd.DataFrame, key: str) -> str | None:
    cands = ALIASES.get(key, [key])
    for c in cands:
        if c in df.columns:
            return c
    return None


def _to_numeric_safe(series: pd.Series) -> pd.Series:
    x = series.map(lambda v: _strip(v))
    x = x.str.replace("\xa0", " ", regex=False).str.replace(" ", "", regex=False)
    x = x.replace({"": None, "nan": None, "NaN": None, "N/A": None, "None": None})
    return pd.to_numeric(x, errors="coerce")


def _fmt_num(v, unit: str = "", decimals: int = 2) -> str:
    if v is None:
        return "N/A"
    try:
        fv = float(v)
        if pd.isna(fv):
            return "N/A"
        if abs(fv) >= 1000:
            s = f"{fv:,.0f}"
        else:
            s = f"{fv:,.{decimals}f}"
        s = s.replace(",", ".")
        return s + (f" {unit}" if unit else "")
    except Exception:
        return _strip(v) + (f" {unit}" if unit else "")


def _as_stmt_preview(v, limit: int) -> tuple[str, str]:
    """
    Retorna:
      preview: 1 linha, compactada, truncada
      full: mantém quebras (prettier pro <pre>) sem quebrar layout
    """
    full_raw = _strip(v).replace("\r\n", "\n").replace("\r", "\n")

    # preview compactado (1-linha)
    full_one_line = " ".join(full_raw.split())
    preview = full_one_line[:limit]
    if len(full_one_line) > limit:
        preview += " …"

    return preview, full_raw


def _df_to_html_table(df: pd.DataFrame, title: str) -> str:
    if df is None or df.empty:
        return f"""
        <div class="card">
          <div class="card-title">{_html_escape(title)}</div>
          <div class="note">Sem dados.</div>
        </div>
        """

    # NÃO exibir colunas internas (prefixo "_")
    display_cols = [c for c in df.columns if not str(c).startswith("_")]

    head = "".join([f"<th>{_html_escape(c)}</th>" for c in display_cols])

    rows_html = []
    for _, r in df.iterrows():
        tds = []
        for c in display_cols:
            val = r.get(c, "")

            if c == "statement":
                preview = _strip(val)
                full = _strip(r.get("_statement_full", ""))

                # preview + expandir com SQL completo (não deforma)
                cell = f"""
                <div class="stmt-preview">{_html_escape(preview)}</div>
                <details class="stmt-details">
                  <summary>ver completo</summary>
                  <pre class="stmt-full">{_html_escape(full)}</pre>
                </details>
                """
                tds.append(f"<td>{cell}</td>")
            else:
                tds.append(f"<td>{_html_escape(val)}</td>")

        rows_html.append("<tr>" + "".join(tds) + "</tr>")

    return f"""
    <div class="card">
      <div class="card-title">{_html_escape(title)}</div>
      <div class="tablewrap">
        <table class="tbl">
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
      </div>
    </div>
    """


# =========================
# Detect dataset
# =========================
def _detect_dataset_from_df(df: pd.DataFrame) -> str | None:
    if "dataset" in df.columns:
        vals = df["dataset"].dropna().astype(str).unique().tolist()
        if len(vals) == 1:
            return vals[0].strip()

    cols = set(df.columns)

    if {"calls", "total_time_s", "avg_time_s", "p95_time_s", "worst_time_s"}.issubset(cols) and _pick_first_col(df, "statement"):
        return "top_sql_calls"

    if {"calls", "total_query_time_s", "worst_query_time_s"}.issubset(cols) and _pick_first_col(df, "statement"):
        return "top_sql_total_time"

    if {"calls", "total_lock_time_s"}.issubset(cols) and _pick_first_col(df, "statement"):
        return "top_sql_lock_total"

    if {"worst_query_time_s", "calls"}.issubset(cols) and _pick_first_col(df, "statement"):
        return "top_sql_worst_single"

    if {"calls", "total_rows_examined"}.issubset(cols) and _pick_first_col(df, "statement"):
        return "top_sql_rows_examined"

    if {"calls", "total_query_time_s"}.issubset(cols) and _pick_first_col(df, "db_user"):
        return "top_users_total_time"

    if {"calls", "total_query_time_s"}.issubset(cols) and _pick_first_col(df, "src_ip"):
        return "top_srcip_total_time"

    return None


# =========================
# Normalize per dataset
# =========================
def _normalize_dataset_df(df: pd.DataFrame, dataset: str, stmt_limit: int) -> pd.DataFrame:
    out = _norm_cols(df)
    if out.empty:
        return out

    if dataset.startswith("top_sql_"):
        stmt_col = _pick_first_col(out, "statement")
        if stmt_col and stmt_col != "statement":
            out = out.rename(columns={stmt_col: "statement"})

    if dataset == "top_users_total_time":
        ucol = _pick_first_col(out, "db_user")
        if ucol and ucol != "db_user":
            out = out.rename(columns={ucol: "db_user"})

    if dataset == "top_srcip_total_time":
        ipcol = _pick_first_col(out, "src_ip")
        if ipcol and ipcol != "src_ip":
            out = out.rename(columns={ipcol: "src_ip"})

    for c in list(out.columns):
        if c in NUMERIC_COLS:
            out[c] = _to_numeric_safe(out[c])

    # statement preview + full (coluna interna)
    if "statement" in out.columns:
        previews, fulls = [], []
        for v in out["statement"].tolist():
            p, f = _as_stmt_preview(v, stmt_limit)
            previews.append(p)
            fulls.append(f)
        out["_statement_full"] = fulls
        out["statement"] = previews

    return out


def _select_and_format(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    exist = [c for c in cols if c in df.columns]
    out = df[exist].copy() if exist else pd.DataFrame()

    for c in exist:
        if c in {"total_rows_examined", "avg_rows_examined", "worst_rows_examined", "calls"}:
            out[c] = df[c].map(lambda v: _fmt_num(v, "", decimals=0))
        elif c.endswith("_time_s") or c.endswith("_query_time_s") or c.endswith("_lock_time_s"):
            out[c] = df[c].map(lambda v: _fmt_num(v, "s", decimals=2))
        else:
            pass

    return out


# =========================
# Paths
# =========================
def _strip_cfg(x) -> str:
    return "" if x is None else str(x).strip()


def _resolve_week(cfg: dict, override_week: str) -> str:
    week = _strip_cfg(override_week) or _strip_cfg(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter week: YYYY-MM-DD (ou passe --week).")
    return week


def _resolve_in_dir(run_yaml: Path, cfg: dict, week: str, in_dir_arg: str) -> Path:
    if _strip_cfg(in_dir_arg):
        return Path(in_dir_arg)

    p = run_yaml.parent / "layerC" / "collected" / week
    if p.exists():
        return p

    return run_yaml.parent / "layerC" / "inputs" / week


def _resolve_out_html(run_yaml: Path, week: str, out_arg: str) -> Path:
    if _strip_cfg(out_arg):
        return Path(out_arg)
    html_dir = run_yaml.parent / "reports" / "html" / week
    html_dir.mkdir(parents=True, exist_ok=True)
    return html_dir / "layerC_cloudwatch_slowlog.html"


# =========================
# MAIN
# =========================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True, help="Caminho do run.yaml da instância")
    ap.add_argument("--week", default="", help="Override week (YYYY-MM-DD)")
    ap.add_argument("--in-dir", default="", help="Override diretório de entrada (normalizados)")
    ap.add_argument("--out", default="", help="Arquivo HTML de saída (opcional)")
    ap.add_argument("--stmt-chars", type=int, default=STMT_PREVIEW_CHARS_DEFAULT, help="Chars do preview do statement")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip_cfg(cfg.get("instance")) or run_yaml.parent.name
    week = _resolve_week(cfg, args.week)

    in_dir = _resolve_in_dir(run_yaml, cfg, week, args.in_dir)
    if not in_dir.exists():
        raise SystemExit(f"Diretório de entrada não encontrado: {in_dir}")

    out_html = _resolve_out_html(run_yaml, week, args.out)

    files = sorted([p for p in in_dir.glob("*.csv") if p.is_file() and p.stat().st_size > 0])
    if not files:
        raise SystemExit(f"Nenhum CSV encontrado em: {in_dir}")

    grouped: dict[str, list[pd.DataFrame]] = {}
    manifest_rows = []

    for f in files:
        df = _read_csv_any(f)
        df = _norm_cols(df)
        if df.empty:
            manifest_rows.append({"file": f.name, "dataset": "", "rows": 0, "note": "vazio/falha leitura"})
            continue

        ds = _detect_dataset_from_df(df)
        if not ds:
            manifest_rows.append({"file": f.name, "dataset": "", "rows": int(df.shape[0]), "note": "dataset não reconhecido"})
            continue

        df2 = _normalize_dataset_df(df, ds, stmt_limit=args.stmt_chars)
        grouped.setdefault(ds, []).append(df2)
        manifest_rows.append({"file": f.name, "dataset": ds, "rows": int(df2.shape[0]), "note": ""})

    blocks = []
    missing = []

    for ds_key, title, cols in DATASET_SPECS:
        if ds_key not in grouped:
            missing.append(ds_key)
            continue

        merged = pd.concat(grouped[ds_key], ignore_index=True)

        # Garante coluna interna _statement_full (não será exibida)
        if "statement" in merged.columns and "_statement_full" not in merged.columns:
            merged["_statement_full"] = merged["statement"].map(lambda s: _strip(s))

        disp = _select_and_format(merged, cols)

        # Reanexa _statement_full como coluna interna (prefixo "_") para o renderer usar no <details>
        if "statement" in disp.columns and "_statement_full" in merged.columns:
            disp["_statement_full"] = merged["_statement_full"].tolist()

        blocks.append(_df_to_html_table(disp, title))

    man_df = pd.DataFrame(manifest_rows)
    man_html = _df_to_html_table(man_df, "Manifest — arquivos lidos/detectados")

    missing_html = ""
    if missing:
        missing_html = "<br/>".join([_html_escape(f"• {m}") for m in missing])

    now_local = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "Camada C (CloudWatch Logs Insights)"
    subtitle = f"Instância: {instance} · Semana: {week} · Gerado em: {now_local}"

    css = f"""
    <style>
      :root {{ --bg:#0b0f14; --panel:#111826; --panel2:#0f1622; --text:#e6eefc; --muted:#9fb1cc; --border:#223048; }}
      body {{ margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial; background:var(--bg); color:var(--text); }}
      .wrap {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
      h1 {{ font-size: 22px; margin: 0 0 6px 0; }}
      .sub {{ color:var(--muted); font-size: 13px; margin-bottom: 14px; }}
      .card {{ background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 1px solid var(--border); border-radius: 16px; padding: 14px; margin-top:14px; }}
      .card-title {{ font-weight: 700; margin-bottom: 10px; color: #d7e5ff; }}
      .note {{ color: var(--muted); font-size: 12px; line-height: 1.45; }}
      .tablewrap {{ overflow:auto; }}
      table.tbl {{ width:100%; border-collapse: collapse; font-size: 13px; }}
      table.tbl th, table.tbl td {{ padding: 8px 8px; border-bottom: 1px solid rgba(255,255,255,.06); text-align: left; vertical-align: top; }}
      table.tbl th {{ color: var(--muted); font-weight: 600; }}

      /* statement cell */
      .stmt-preview {{
        white-space: normal;
        word-break: break-word;
        max-width: 760px;
        line-height: 1.35;
      }}
      .stmt-details summary {{
        cursor: pointer;
        color: #9fb1cc;
        margin-top: 6px;
        font-size: 12px;
      }}
      .stmt-full {{
        margin-top: 8px;
        padding: 10px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,.10);
        background: rgba(0,0,0,.20);
        color: #e6eefc;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 260px;     /* NÃO deforma: vira scroll */
        overflow: auto;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        font-size: 12px;
        line-height: 1.35;
      }}

      .pill {{
        display:inline-block; padding:2px 8px; border-radius:999px;
        border:1px solid rgba(255,255,255,.12); color:var(--muted); font-size:12px;
      }}
    </style>
    """

    missing_block = ""
    if missing_html:
        missing_block = f"""
        <div class="card">
          <div class="card-title">Cobertura (datasets ausentes)</div>
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
      <div class="note">
        Esta camada organiza as “piores” queries do slow log por: tempo total, calls, lock_time, pior execução individual e rows_examined.
        <br/><br/>
        <span class="pill">Preview: {args.stmt_chars} chars</span>
        <span class="pill">SQL completo: clique em “ver completo”</span>
      </div>
    </div>

    {missing_block}

    {''.join(blocks)}

    {man_html}

    <div class="card">
      <div class="card-title">Nota</div>
      <div class="note">
        Se aparecerem picos de tempo/lock/rows_examined, a causa-raiz costuma ficar na Camada B (índices/fragmentação/volumetria)
        e no desenho das queries (plano/EXPLAIN).
      </div>
    </div>

  </div>
</body>
</html>
"""
    out_html.write_text(html, encoding="utf-8")
    print(f"[OK] report gerado: {out_html}")
    print(f"[OK] lendo CSVs de: {in_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
