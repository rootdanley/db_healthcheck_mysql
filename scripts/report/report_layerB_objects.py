#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
import yaml


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


def _read_csv(path: Path) -> pd.DataFrame:
    if (not path.exists()) or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=object)
    except Exception:
        return pd.DataFrame()


def _fmt_num(v, decimals=2) -> str:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "N/A"
        fv = float(str(v).replace(",", "."))
        if pd.isna(fv):
            return "N/A"
        if decimals == 0:
            s = f"{fv:,.0f}"
        else:
            s = f"{fv:,.{decimals}f}"
        return s.replace(",", ".")
    except Exception:
        return _strip(v) or "N/A"


def _df_to_table(df: pd.DataFrame, *, title: str, note: str = "", max_rows: int | None = None,
                 wide_cols: set[str] | None = None) -> str:
    if df is None or df.empty:
        return f"""
        <div class="card">
          <div class="card-title">{_html_escape(title)}</div>
          <div class="note">{_html_escape(note) if note else "Sem dados."}</div>
        </div>
        """

    if max_rows is not None and len(df) > max_rows:
        df = df.head(max_rows).copy()

    cols = list(df.columns)

    head = "".join([f"<th>{_html_escape(c)}</th>" for c in cols])
    body_rows = []

    wide_cols = wide_cols or set()

    for _, r in df.iterrows():
        tds = []
        for c in cols:
            val = r.get(c, "")
            # Colunas longas (listas etc) -> details
            if c in wide_cols:
                full = _strip(val)
                preview = full[:220] + (" …" if len(full) > 220 else "")
                tds.append(
                    "<td>"
                    f"<details class='details'>"
                    f"<summary><span class='mono'>{_html_escape(preview)}</span></summary>"
                    f"<div class='mono block'>{_html_escape(full)}</div>"
                    f"</details>"
                    "</td>"
                )
            else:
                tds.append(f"<td>{_html_escape(val)}</td>")
        body_rows.append("<tr>" + "".join(tds) + "</tr>")

    return f"""
    <div class="card">
      <div class="card-title">{_html_escape(title)}</div>
      {f'<div class="note">{_html_escape(note)}</div>' if note else ''}
      <div class="tablewrap">
        <table class="tbl">
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </div>
    </div>
    """


def _kv_from_instance_facts(df: pd.DataFrame) -> pd.DataFrame:
    """
    instance_facts.csv é 1 linha com muitos campos.
    Converte pra 2 colunas: key/value (fica legível).
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["key", "value"])
    row = df.iloc[0].to_dict()
    out = pd.DataFrame([{"key": k, "value": _strip(v)} for k, v in row.items()])
    return out


def _resolve_week(cfg: dict, override_week: str) -> str:
    week = _strip(override_week) or _strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter week: YYYY-MM-DD (ou passe --week).")
    return week


def _in_dir(run_yaml: Path, week: str, in_arg: str) -> Path:
    if _strip(in_arg):
        return Path(in_arg)
    return run_yaml.parent / "layerB" / "collected" / week


def _out_html(run_yaml: Path, week: str, out_arg: str) -> Path:
    if _strip(out_arg):
        p = Path(out_arg)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    p = run_yaml.parent / "reports" / "html" / week / "layerB_objects.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True)
    ap.add_argument("--week", default="")
    ap.add_argument("--in-dir", default="", help="default: layerB/collected/<week>")
    ap.add_argument("--out", default="", help="default: reports/html/<week>/layerB_objects.html")
    ap.add_argument("--limit", type=int, default=50, help="limite padrão de linhas por tabela")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip(cfg.get("instance")) or run_yaml.parent.name
    week = _resolve_week(cfg, args.week)

    in_dir = _in_dir(run_yaml, week, args.in_dir)
    if not in_dir.exists():
        raise SystemExit(f"Diretório de entrada não encontrado: {in_dir}")

    out_path = _out_html(run_yaml, week, args.out)

    # CSVs esperados (gerados pelo normalizador)
    p_instance = in_dir / "instance_facts.csv"
    p_sizes = in_dir / "db_sizes.csv"
    p_tables = in_dir / "top_tables.csv"
    p_indexes = in_dir / "top_indexes.csv"
    p_frag = in_dir / "table_fragmentation.csv"
    p_unused = in_dir / "unused_indexes.csv"
    p_scans = in_dir / "table_scans.csv"
    p_idx_by_tbl = in_dir / "top_indexes_by_tables.csv"  # normalizador gera esse nome

    df_instance = _read_csv(p_instance)
    df_sizes = _read_csv(p_sizes)
    df_tables = _read_csv(p_tables)
    df_indexes = _read_csv(p_indexes)
    df_frag = _read_csv(p_frag)
    df_unused = _read_csv(p_unused)
    df_scans = _read_csv(p_scans)
    df_idx_by_tbl = _read_csv(p_idx_by_tbl)

    # Formata alguns numéricos (se existirem)
    def fmt_cols(df: pd.DataFrame, mapping: dict[str, int]) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        out = df.copy()
        for c, dec in mapping.items():
            if c in out.columns:
                out[c] = out[c].map(lambda v: _fmt_num(v, decimals=dec))
        return out

    df_sizes = fmt_cols(df_sizes, {"size_mb": 2, "size_gb": 4})
    df_tables = fmt_cols(df_tables, {"table_rows": 0, "total_mb": 2, "data_mb": 2, "index_mb": 2})
    df_indexes = fmt_cols(df_indexes, {"pages": 0, "approx_index_mb": 2})
    df_frag = fmt_cols(df_frag, {"data_free_mb": 2, "total_mb": 2, "free_pct": 2})
    df_unused = fmt_cols(df_unused, {"approx_index_mb": 2})
    df_scans = fmt_cols(df_scans, {"rows_full_scanned": 0})
    df_idx_by_tbl = fmt_cols(df_idx_by_tbl, {"index_count": 0})

    # Heurísticas bem simples (só pra não ficar “mudo”)
    heur = []
    if not df_frag.empty and "free_pct" in df_frag.columns:
        try:
            fp = pd.to_numeric(df_frag["free_pct"], errors="coerce").dropna()
            if not fp.empty and fp.quantile(0.95) >= 20:
                heur.append(f"Possível fragmentação: free_pct p95={fp.quantile(0.95):.2f}% (>=20%).")
        except Exception:
            pass

    if not df_idx_by_tbl.empty and "index_count" in df_idx_by_tbl.columns:
        try:
            ic = pd.to_numeric(df_idx_by_tbl["index_count"], errors="coerce").dropna()
            if not ic.empty and ic.max() >= 20:
                heur.append(f"Muitas tabelas com muitos índices: max index_count={int(ic.max())} (atenção para INSERT/UPDATE).")
        except Exception:
            pass

    heur_html = "<div class='note'>• Nenhuma anomalia óbvia encontrada nas heurísticas da Camada B (confirmar com Camada C/workload real).</div>"
    if heur:
        heur_html = "<div class='note'>" + "<br/>".join([_html_escape("• " + h) for h in heur]) + "</div>"

    # instance facts como KV
    df_instance_kv = _kv_from_instance_facts(df_instance)

    blocks = []
    blocks.append(f"""
    <div class="card">
      <div class="card-title">Pré-análises automáticas (heurísticas)</div>
      {heur_html}
    </div>
    """)

    blocks.append(_df_to_table(df_instance_kv, title="Instance Facts (Camada B)", note="Fonte: instance_facts"))
    blocks.append(_df_to_table(df_sizes, title="Volumetria por Database", note="Fonte: db_sizes", max_rows=args.limit))
    blocks.append(_df_to_table(df_tables, title="Top 10 tabelas por tamanho", note="Fonte: top_tables", max_rows=10))
    blocks.append(_df_to_table(df_indexes, title="Top 10 índices por tamanho", note="Fonte: top_indexes", max_rows=10))
    blocks.append(_df_to_table(df_unused, title="Índices possivelmente não usados (atenção à janela)", note="Fonte: unused_indexes (sys/performance_schema)", max_rows=args.limit))
    blocks.append(_df_to_table(df_frag, title="Sinais de fragmentação (data_free / total)", note="Fonte: table_fragmentation", max_rows=args.limit))
    blocks.append(_df_to_table(df_scans, title="Top table scans (full scan)", note="Se estiver vazio: janela curta, pouco workload, ou sys/performance_schema sem dados.", max_rows=args.limit))
    blocks.append(_df_to_table(
        df_idx_by_tbl,
        title="Top indexes (detalhado por tabela)",
        note="Fonte: top_indexes_by_tables (lista de índices por tabela).",
        max_rows=args.limit,
        wide_cols={"index_names"},
    ))

    now_local = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "Camada B (Volumetria e Saúde de Objetos)"
    subtitle = f"Instância: {instance} · Semana: {week} · Gerado em: {now_local}"

    css = """
    <style>
      :root { --bg:#0b0f14; --panel:#111826; --panel2:#0f1622; --text:#e6eefc; --muted:#9fb1cc; --border:#223048; }
      body { margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial; background:var(--bg); color:var(--text); }
      .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
      h1 { font-size: 22px; margin: 0 0 6px 0; }
      .sub { color:var(--muted); font-size: 13px; margin-bottom: 14px; }
      .card { background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 1px solid var(--border); border-radius: 16px; padding: 14px; margin-top:14px; }
      .card-title { font-weight: 700; margin-bottom: 10px; color: #d7e5ff; }
      .note { color: var(--muted); font-size: 12px; line-height: 1.45; }
      .tablewrap { overflow:auto; }
      table.tbl { width:100%; border-collapse: collapse; font-size: 13px; }
      table.tbl th, table.tbl td { padding: 8px 8px; border-bottom: 1px solid rgba(255,255,255,.06); text-align: left; vertical-align: top; }
      table.tbl th { color: var(--muted); font-weight: 600; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
      .block { margin-top: 8px; white-space: pre-wrap; word-break: break-word; }
      details.details summary { cursor: pointer; color: #cfe3ff; }
      details.details { border: 1px solid rgba(255,255,255,.10); border-radius: 10px; padding: 8px; background: rgba(255,255,255,.02); }
    </style>
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

    {''.join(blocks)}

    <div class="card">
      <div class="card-title">Nota</div>
      <div class="note">
        A Camada B mostra “estrutura” (tabelas/índices/fragmentação). A causa-raiz de consumo costuma aparecer na Camada C
        (top SQL / lock_time / piores picos) e na análise de plano (EXPLAIN).
      </div>
    </div>

  </div>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    print(f"[OK] report gerado: {out_path}")
    print(f"[OK] lendo CSVs de: {in_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
