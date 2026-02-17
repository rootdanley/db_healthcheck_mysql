#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import datetime as dt
import json
import re
from pathlib import Path

import yaml


def _as_str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return str(x)


def _strip(x) -> str:
    return _as_str(x).strip()


def _read_single_row_csv(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            return row
    return {}


def _fmt_yesno_pt(v: str) -> str:
    s = _strip(v).lower()
    if s == "yes":
        return "Sim"
    if s == "no":
        return "Não"
    return _strip(v)


def _fmt_int(v: str) -> str:
    s = _strip(v)
    if not s:
        return ""
    try:
        return f"{int(float(s))}"
    except Exception:
        return s


def _fmt_days(v) -> str:
    # vazio => não exibe linha (combina com _kv_table)
    if v is None or _strip(v) == "":
        return ""
    try:
        n = int(float(v))
    except Exception:
        return ""
    return f"{n} dias" if n >= 0 else f"{n} dias (já passou)"


def _warning_style(level: str) -> tuple[str, str]:
    level = _strip(level).upper()
    if level == "CRITICAL":
        return ("#3b0b0b", "#ffd7d7")
    if level == "WARNING":
        return ("#3b2a00", "#ffe7b5")
    return ("#0b2a12", "#caffd8")


def _level_label_pt(level: str) -> str:
    level = _strip(level).upper()
    if level == "CRITICAL":
        return "CRÍTICO"
    if level == "WARNING":
        return "ATENÇÃO"
    return "OK"


def _to_int_or_none(v):
    s = _strip(v)
    if not s:
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def _compute_warning_pt(row: dict) -> tuple[str, str]:
    """
    Regras pedidas (pt-br):
      - minor_days < 30  => CRITICAL
      - minor_days < 50  => WARNING
    + mensagens em pt-br
    """
    reasons = []
    level = "OK"

    minor_days = _to_int_or_none(row.get("minor_days_to_end_of_standard_support", ""))
    minor_end = _strip(row.get("minor_end_of_standard_support_date", ""))

    # Regra principal
    if minor_days is not None:
        if minor_days < 0:
            level = "CRITICAL"
            reasons.append(
                f"Suporte padrão da versão minor já expirou há {abs(minor_days)} dias"
                + (f" (data: {minor_end})" if minor_end else "")
                + "."
            )
        elif minor_days < 30:
            level = "CRITICAL"
            reasons.append(
                f"Suporte padrão da versão minor expira em {minor_days} dias"
                + (f" (data: {minor_end})" if minor_end else "")
                + "."
            )
        elif minor_days < 50:
            level = "WARNING"
            reasons.append(
                f"Suporte padrão da versão minor expira em {minor_days} dias"
                + (f" (data: {minor_end})" if minor_end else "")
                + "."
            )

    # Observações úteis (pt-br) quando está em WARNING/CRITICAL
    if level in ("WARNING", "CRITICAL"):
        ext = _strip(row.get("extended_support_enabled", ""))
        auto_minor = _strip(row.get("auto_minor_version_upgrade", ""))

        if ext.lower() == "no":
            reasons.append(
                "Extended Support está desabilitado: recomenda-se atualizar antes do fim do suporte padrão."
            )
        if auto_minor.lower() == "no":
            reasons.append(
                "Auto minor version upgrade está desabilitado: patches/atualizações menores devem ser aplicados manualmente na janela de manutenção."
            )

    if not reasons:
        return ("OK", "Sem alertas relevantes.")

    return (level, " ".join(reasons))


def _safe_json_load(s: str) -> dict:
    if not _strip(s):
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _html_escape(s: str) -> str:
    s = _as_str(s)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def _kv_table(title: str, rows: list[tuple[str, str]]) -> str:
    cleaned = []
    for k, v in rows:
        if _strip(v) == "":
            continue
        cleaned.append((k, v))

    trs = []
    if not cleaned:
        trs.append("<tr><td class='k'>N/A</td><td class='v'>N/A</td></tr>")
    else:
        for k, v in cleaned:
            trs.append(
                f"<tr><td class='k'>{_html_escape(k)}</td><td class='v'>{_html_escape(v)}</td></tr>"
            )

    return f"""
    <div class="card">
      <div class="card-title">{_html_escape(title)}</div>
      <table class="kv">
        <tbody>
          {''.join(trs)}
        </tbody>
      </table>
    </div>
    """


def _deployment_label(deployment_type: str) -> str:
    dtp = _strip(deployment_type)
    if dtp == "aurora_serverless_v2":
        return "Aurora Serverless v2"
    if dtp == "aurora_provisioned":
        return "Aurora Provisioned"
    if dtp == "rds_mysql_community":
        return "RDS MySQL Community (Provisioned)"
    return dtp or "N/A"


def _engine_mode_from_yaml(engine_hint: str) -> str:
    h = _strip(engine_hint).lower()
    if "aurora" in h:
        return "aurora"
    return "mysql_community"


def _extract_mysql_major(engine_version: str) -> str:
    m = re.match(r"^\s*(\d+)\.(\d+)\.", _strip(engine_version))
    if not m:
        return ""
    return f"MySQL {m.group(1)}.{m.group(2)}"


def _extract_aurora_minor(engine_version: str) -> str:
    s = _strip(engine_version)
    m = re.search(r"mysql_aurora\.(\d+)\.(\d+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2).zfill(2)}"
    return ""


def _aurora_major_label(aurora_minor: str) -> str:
    s = _strip(aurora_minor)
    if not s or "." not in s:
        return ""
    major = s.split(".", 1)[0]
    return f"Aurora MySQL {major}"


def _compute_keys_from_yaml(engine_hint: str, engine_version: str) -> tuple[str, str, str]:
    mode = _engine_mode_from_yaml(engine_hint)
    if mode == "aurora":
        minor_key = _extract_aurora_minor(engine_version)
        major_key = _aurora_major_label(minor_key)
        return ("Aurora MySQL", minor_key, major_key)

    minor_key = _strip(engine_version)
    major_key = _extract_mysql_major(engine_version)
    return ("RDS MySQL Community", minor_key, major_key)


def build_html(row: dict, instance: str, week: str, engine_hint: str) -> str:
    now_local = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    deployment_type = _strip(row.get("deployment_type", ""))
    deployment_lbl = _deployment_label(deployment_type)

    # ✅ WARNING/CRITICAL calculado aqui (pt-br), com suas regras 30/50
    warning_level, warning_reasons = _compute_warning_pt(row)
    bg, fg = _warning_style(warning_level)

    title = "Camada 0 (Inventário)"
    subtitle = f"Instância: {instance} · Semana: {week} · Tipo: {deployment_lbl} · Gerado em: {now_local}"

    identity = [
        ("DB Identifier", row.get("db_identifier", "")),
        ("DB Name", row.get("db_name", "")),
        ("Role", row.get("role", "")),
        ("Deployment Type", deployment_lbl),
        ("Engine", row.get("engine", "")),
        ("Engine Version", row.get("engine_version", "")),
    ]

    compute = [("Instance Class", row.get("instance_class", ""))]
    if deployment_type == "aurora_serverless_v2":
        compute += [
            ("Min ACU", row.get("min_acu", "")),
            ("Max ACU", row.get("max_acu", "")),
        ]
    else:
        compute += [
            ("vCPU", _fmt_int(row.get("vcpu", ""))),
            ("RAM (GiB)", _fmt_int(row.get("ram_gib", ""))),
        ]

    if deployment_type == "rds_mysql_community":
        storage = [
            ("Storage Type", row.get("storage_type", "")),
            ("Allocated Storage (GiB)", _fmt_int(row.get("allocated_storage_gib", ""))),
            ("Provisioned IOPS", _fmt_int(row.get("provisioned_iops", ""))),
            ("Throughput (MiB/s)", _fmt_int(row.get("storage_throughput_mib", ""))),
            ("Autoscaling", _fmt_yesno_pt(row.get("storage_autoscaling", ""))),
            ("Max Storage Threshold (GiB)", _fmt_int(row.get("max_storage_threshold_gib", ""))),
        ]
    else:
        storage = [
            ("Storage Model", row.get("storage_type", "") or "Aurora cluster volume (auto)"),
            ("Autoscaling", _fmt_yesno_pt(row.get("storage_autoscaling", ""))),
        ]

    security_backup = [
        ("Master Username", row.get("master_username", "")),
        ("Publicly Accessible", _fmt_yesno_pt(row.get("publicly_accessible", ""))),
        ("Deletion Protection", _fmt_yesno_pt(row.get("deletion_protection", ""))),
        ("Backup Retention (days)", _fmt_int(row.get("backup_retention_days", ""))),
    ]

    observability = [
        ("Monitoring (Database Insights)", _fmt_yesno_pt(row.get("monitoring_type", ""))),
        ("Performance Insights Enabled", _fmt_yesno_pt(row.get("performance_insights_enabled", ""))),
        ("PI Retention (days)", _fmt_int(row.get("performance_insights_retention_days", ""))),
        ("Enhanced Monitoring Enabled", _fmt_yesno_pt(row.get("enhanced_monitoring_enabled", ""))),
        ("Enhanced Monitoring Interval (sec)", _fmt_int(row.get("enhanced_monitoring_interval_sec", ""))),
        ("CloudWatch Logs Exported", row.get("logs_exported_cloudwatch", "")),
    ]

    groups = [
        ("Auto Minor Version Upgrade", _fmt_yesno_pt(row.get("auto_minor_version_upgrade", ""))),
        ("RDS Extended Support Enabled", _fmt_yesno_pt(row.get("extended_support_enabled", ""))),
    ]
    if deployment_type.startswith("aurora_"):
        groups = [
            ("DB Cluster Parameter Group", row.get("db_cluster_parameter_group", "")),
            ("DB Instance Parameter Group", row.get("db_instance_parameter_group", "")),
        ] + groups
    else:
        groups = [
            ("DB Parameter Group", row.get("db_parameter_group", "")),
            ("Option Group", row.get("option_group", "")),
        ] + groups

    engine_version = row.get("engine_version", "")
    engine_family_calc, minor_key_calc, major_key_calc = _compute_keys_from_yaml(engine_hint, engine_version)

    is_aurora_yaml = (_engine_mode_from_yaml(engine_hint) == "aurora")
    compatible_mysql = row.get("compatible_mysql", "") if is_aurora_yaml else ""

    y3_date = _strip(row.get("extended_support_year3_pricing_start_date", ""))

    support = [
        ("Engine", row.get("engine_family", "") or engine_family_calc),
        ("Versão Minor", minor_key_calc),
        ("Versão Major", major_key_calc),
        ("MySQL Compatível (Aurora)", compatible_mysql),

        ("Fim do Suporte Padrão (Minor)", row.get("minor_end_of_standard_support_date", "")),
        ("Dias até fim do suporte (Minor)", _fmt_days(row.get("minor_days_to_end_of_standard_support", ""))),

        ("Fim do Suporte Padrão (Major)", row.get("major_end_of_standard_support_date", "")),
        ("Dias até fim do suporte (Major)", _fmt_days(row.get("major_days_to_end_of_standard_support", ""))),

        ("Início Cobrança Sup. Est. Ano 1", row.get("extended_support_year1_pricing_start_date", "")),
        ("Dias até Cobrança Ano 1", _fmt_days(row.get("days_to_extended_support_year1_pricing_start", ""))),

        # ✅ Y3 só aparece se houver data (se não, some tudo)
        ("Início Cobrança Sup. Est. Ano 3", y3_date),
        ("Dias até Cobrança Ano 3", _fmt_days(row.get("days_to_extended_support_year3_pricing_start", "")) if y3_date else ""),

        ("Fim do Suporte Estendido", row.get("end_of_extended_support_date", "")),
        ("Dias até fim do Sup. Estendido", _fmt_days(row.get("days_to_end_of_extended_support", ""))),
    ]

    inv = _safe_json_load(row.get("inventory_json", ""))
    if inv:
        items = sorted(inv.items(), key=lambda kv: kv[0].lower())
        inv_lines = "\n".join(
            [f"<tr><td class='k'>{_html_escape(k)}</td><td class='v'>{_html_escape(v)}</td></tr>"
             for k, v in items]
        )
    else:
        inv_lines = "<tr><td class='k'>inventory_json</td><td class='v'>N/A</td></tr>"

    warning_box = f"""
    <div class="banner" style="background:{bg};color:{fg};">
      <div class="banner-title">Status: { _html_escape(_level_label_pt(warning_level)) }</div>
      <div class="banner-text">{ _html_escape(warning_reasons) }</div>
    </div>
    """

    css = """
    <style>
      :root { --bg:#0b0f14; --panel:#111826; --panel2:#0f1622; --text:#e6eefc; --muted:#9fb1cc; --border:#223048; }
      body { margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial; background:var(--bg); color:var(--text); }
      .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
      h1 { font-size: 22px; margin: 0 0 6px 0; }
      .sub { color:var(--muted); font-size: 13px; margin-bottom: 14px; }
      .banner { border: 1px solid rgba(255,255,255,.12); border-radius: 14px; padding: 14px 16px; margin: 14px 0 18px; }
      .banner-title { font-weight: 700; margin-bottom: 6px; }
      .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
      .grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
      .card { background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 1px solid var(--border); border-radius: 16px; padding: 14px 14px; }
      .card-title { font-weight: 700; margin-bottom: 10px; color: #d7e5ff; }
      table.kv { width:100%; border-collapse: collapse; font-size: 13px; }
      table.kv td { padding: 8px 8px; border-bottom: 1px solid rgba(255,255,255,.06); vertical-align: top; }
      table.kv td.k { color: var(--muted); width: 48%; }
      table.kv td.v { color: var(--text); }
      details { margin-top: 14px; }
      summary { cursor: pointer; color: var(--muted); }
      .foot { margin-top: 18px; color: var(--muted); font-size: 12px; }
      @media (max-width: 980px) {
        .grid, .grid3 { grid-template-columns: 1fr; }
      }
    </style>
    """

    html = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{_html_escape(title)} - { _html_escape(instance) }</title>
  {css}
</head>
<body>
  <div class="wrap">
    <h1>{_html_escape(title)}</h1>
    <div class="sub">{_html_escape(subtitle)}</div>

    {warning_box}

    <div class="grid">
      {_kv_table("Identidade", identity)}
      {_kv_table("Suporte & Lifecycle", support)}
    </div>

    <div class="grid3" style="margin-top:14px;">
      {_kv_table("Compute", compute)}
      {_kv_table("Storage", storage)}
      {_kv_table("Segurança / Backup", security_backup)}
    </div>

    <div class="grid" style="margin-top:14px;">
      {_kv_table("Observabilidade", observability)}
      {_kv_table("Grupos / Config", groups)}
    </div>

    <details>
      <summary>Ver inventário bruto (layer0 inventory_json)</summary>
      <div class="card" style="margin-top:10px;">
        <div class="card-title">Inventory Raw</div>
        <table class="kv"><tbody>
          {inv_lines}
        </tbody></table>
      </div>
    </details>

    <div class="foot">
      Camada 0 pronta. Próximas camadas (A/B/C) entram depois como novas seções no mesmo HTML.
    </div>
  </div>
</body>
</html>
"""
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True, help="run.yaml da instância")
    ap.add_argument("--out", default="", help="arquivo html de saída (opcional)")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip(cfg.get("instance")) or run_yaml.parent.name
    week = _strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter 'week: YYYY-MM-DD'.")

    engine_hint = _strip(cfg.get("engine", ""))

    collected = run_yaml.parent / "layer0" / "collected" / week / "layer0_collected.csv"
    row = _read_single_row_csv(collected)
    if not row:
        raise SystemExit(f"Não encontrei dados em: {collected}")

    out = Path(args.out) if args.out else (run_yaml.parent / "reports" / "html" / week / "layer0_inventory.html")
    out.parent.mkdir(parents=True, exist_ok=True)

    html = build_html(row, instance=instance, week=week, engine_hint=engine_hint)
    out.write_text(html, encoding="utf-8")
    print(f"[OK] report gerado: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
