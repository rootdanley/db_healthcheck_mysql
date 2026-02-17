#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Relatório Completo (1 arquivo, self-contained)

Lê HTMLs por camada em:
  instances/<inst>/reports/html/<week>/
    - layer0_inventory.html
    - layerA_cloudwatch.html
    - layerB_objects.html
    - layerC_cloudwatch_slowlog.html

Gera 1 HTML final em:
  instances/<inst>/reports/html/<week>/healthcheck_full.html

Estratégia:
- Embute cada camada como base64 no HTML final
- No navegador, converte base64 -> Blob URL (blob:) e seta iframe.src = blob:
  (evita limite/truncamento de data: em src de iframe)
- Mantém botão para abrir:
  (A) camada embutida (blob)
  (B) HTML original do diretório (relativo)
"""

import argparse
import base64
import datetime as dt
from pathlib import Path

import yaml


LAYER_FILES = [
    ("layer0", "Camada 0 — Inventário", "layer0_inventory.html"),
    ("layerA", "Camada A — CloudWatch", "layerA_cloudwatch.html"),
    ("layerB", "Camada B — Objetos", "layerB_objects.html"),
    ("layerC", "Camada C — Slow Log", "layerC_cloudwatch_slowlog.html"),
]


def _strip(x) -> str:
    return "" if x is None else str(x).strip()


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _resolve_week(cfg: dict, override_week: str) -> str:
    week = _strip(override_week) or _strip(cfg.get("week"))
    if not week:
        raise SystemExit("run.yaml precisa ter week: YYYY-MM-DD (ou passe --week).")
    return week


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-yaml", required=True, help="Caminho do run.yaml da instância")
    ap.add_argument("--week", default="", help="Override week (YYYY-MM-DD)")
    ap.add_argument("--out", default="", help="Override caminho do HTML final")
    args = ap.parse_args()

    run_yaml = Path(args.run_yaml)
    cfg = yaml.safe_load(run_yaml.read_text(encoding="utf-8")) or {}

    instance = _strip(cfg.get("instance")) or run_yaml.parent.name
    week = _resolve_week(cfg, args.week)

    html_dir = run_yaml.parent / "reports" / "html" / week
    if not html_dir.exists():
        raise SystemExit(f"Diretório de reports/html não encontrado: {html_dir}")

    out_path = Path(args.out) if _strip(args.out) else (html_dir / "healthcheck_full.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    layers = {}
    missing = []

    for layer_id, layer_title, filename in LAYER_FILES:
        p = html_dir / filename
        if not p.exists() or p.stat().st_size == 0:
            missing.append(filename)
            layers[layer_id] = {
                "title": layer_title,
                "filename": filename,
                "present": False,
                "b64": "",
            }
            continue

        html = _read_text(p)
        layers[layer_id] = {
            "title": layer_title,
            "filename": filename,
            "present": True,
            "b64": _b64(html),
        }

    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # monta JS object
    # evita JSON complex: string b64 é safe.
    js_layers_lines = []
    for layer_id, meta in layers.items():
        title = meta["title"].replace("\\", "\\\\").replace("'", "\\'")
        fn = meta["filename"].replace("\\", "\\\\").replace("'", "\\'")
        b64 = meta["b64"]
        present = "true" if meta["present"] else "false"
        js_layers_lines.append(
            f"  '{layer_id}': {{ title: '{title}', filename: '{fn}', present: {present}, b64: '{b64}' }},"
        )
    js_layers = "{\n" + "\n".join(js_layers_lines) + "\n}"

    missing_js = "[" + ",".join([f"'{m.replace('\\\\','\\\\\\\\').replace('\'','\\\\\'')}'" for m in missing]) + "]"

    hub_html = f"""<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Health Check MySQL — Relatório Completo - {instance}</title>
  <style>
    :root {{
      --bg:#0b0f14; --panel:#111826; --panel2:#0f1622; --text:#e6eefc; --muted:#9fb1cc; --border:#223048;
      --accent:#6ea8fe;
    }}
    body {{ margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial; background:var(--bg); color:var(--text); }}
    .layout {{ display:flex; min-height:100vh; }}
    .side {{
      width: 320px; flex: 0 0 320px;
      background: linear-gradient(180deg, #0e1522, #0b0f14);
      border-right: 1px solid var(--border);
      padding: 18px;
      position: sticky; top: 0; height: 100vh; overflow:auto;
    }}
    .brand h1 {{ font-size: 16px; margin: 0 0 10px 0; }}
    .meta {{ color: var(--muted); font-size: 12px; line-height: 1.4; margin-bottom: 14px; }}
    .navbtn {{
      width:100%; text-align:left;
      padding: 12px 12px;
      margin: 8px 0;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.03);
      color: var(--text);
      cursor: pointer;
    }}
    .navbtn:hover {{ border-color: rgba(110,168,254,.45); }}
    .navbtn.active {{
      border-color: rgba(110,168,254,.75);
      box-shadow: 0 0 0 1px rgba(110,168,254,.25) inset;
      background: rgba(110,168,254,.08);
    }}
    .hint {{ color: var(--muted); font-size: 12px; margin-top: 10px; line-height:1.4; }}
    code {{ color: #cfe3ff; }}

    .main {{
      flex: 1;
      padding: 18px;
      display:flex;
      flex-direction: column;
      gap: 14px;
      min-width: 0;
    }}

    .topbar {{
      display:flex; align-items:center; justify-content:space-between;
      border: 1px solid var(--border);
      background: linear-gradient(180deg, var(--panel), var(--panel2));
      border-radius: 16px;
      padding: 14px 14px;
    }}
    .title h2 {{ margin: 0; font-size: 16px; }}
    .title .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .tools {{ display:flex; gap: 10px; align-items:center; flex-wrap: wrap; }}
    .toolbtn {{
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(255,255,255,.03);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 999px;
      cursor:pointer;
      font-size: 12px;
      white-space: nowrap;
    }}
    .toolbtn:hover {{ border-color: rgba(110,168,254,.45); }}

    .panel {{
      flex: 1;
      min-height: 0;
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
      background: #0b0f14;
      position: relative;
    }}

    .loading {{
      position:absolute; inset:0;
      display:none;
      align-items:center; justify-content:center;
      background: rgba(11,15,20,.55);
      backdrop-filter: blur(2px);
      z-index: 10;
      color: var(--muted);
      font-size: 13px;
    }}
    .loading.show {{ display:flex; }}

    iframe {{
      width: 100%;
      height: 100%;
      border: 0;
      display: none;
      background: #0b0f14;
    }}
    iframe.active {{ display:block; }}

    .box {{
      border: 1px dashed rgba(255,255,255,.18);
      border-radius: 14px;
      padding: 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
  </style>
</head>
<body>
  <div class="layout">
    <aside class="side">
      <div class="brand">
        <h1>Health Check MySQL — Relatório Completo</h1>
      </div>
      <div class="meta">
        <div><b>Instância:</b> {instance}</div>
        <div><b>Semana:</b> {week}</div>
        <div><b>Gerado:</b> {generated_at}</div>
      </div>

      <button class="navbtn" data-layer="layer0">Camada 0 — Inventário</button>
      <button class="navbtn" data-layer="layerA">Camada A — CloudWatch</button>
      <button class="navbtn" data-layer="layerB">Camada B — Objetos</button>
      <button class="navbtn" data-layer="layerC">Camada C — Slow Log</button>
    </aside>

    <main class="main">
      <div class="topbar">
        <div class="title">
          <h2>Health Check MySQL — Relatório Completo</h2>
          <div class="sub">Instância: {instance} · Semana: {week} · Gerado em: {generated_at}</div>
        </div>
        <div class="tools">
          <button class="toolbtn" id="btnOpenEmbedded">Abrir esta camada (embutida)</button>
        </div>
      </div>

      <div class="panel">
        <div class="loading" id="loading">Carregando camada…</div>
        <iframe id="frame_layer0" title="Camada 0"></iframe>
        <iframe id="frame_layerA" title="Camada A"></iframe>
        <iframe id="frame_layerB" title="Camada B"></iframe>
        <iframe id="frame_layerC" title="Camada C"></iframe>
      </div>

      <div class="box" id="missingBox" style="display:none;"></div>

      <div class="box">
        <b>Nota</b><br/>
        Este arquivo é <u>self-contained</u>. Ele não depende de caminhos externos para renderizar.
        Os HTMLs originais continuam aqui: <code>{html_dir}</code>.
      </div>
    </main>
  </div>

<script>
(function() {{
  const LAYERS = {js_layers};
  const missing = {missing_js};

  let current = null;
  const blobUrls = {{}}; // cache: layerId -> blobUrl
  const loading = document.getElementById('loading');

  function showLoading(on) {{
    loading.classList.toggle('show', !!on);
  }}

  function setNavActive(layerId) {{
    document.querySelectorAll('.navbtn').forEach(b => {{
      b.classList.toggle('active', b.getAttribute('data-layer') === layerId);
    }});
    document.querySelectorAll('iframe').forEach(fr => {{
      fr.classList.toggle('active', fr.id === ('frame_' + layerId));
    }});
  }}

  async function ensureBlobUrl(layerId) {{
    if (blobUrls[layerId]) return blobUrls[layerId];
    const meta = LAYERS[layerId];
    if (!meta || !meta.present || !meta.b64) return '';

    // Converte base64 -> Blob usando fetch(data:) e gera URL.createObjectURL(blob)
    // Isso evita colocar data: enorme no src do iframe (que costuma falhar).
    const resp = await fetch('data:text/html;charset=utf-8;base64,' + meta.b64);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    blobUrls[layerId] = url;
    return url;
  }}

  async function activate(layerId) {{
    if (!LAYERS[layerId]) layerId = 'layer0';
    current = layerId;
    setNavActive(layerId);

    if (location.hash !== '#' + layerId) {{
      history.replaceState(null, '', '#' + layerId);
    }}

    const fr = document.getElementById('frame_' + layerId);
    if (!fr) return;

    // se já tem src, não recarrega
    if (fr.getAttribute('src')) return;

    showLoading(true);
    try {{
      const url = await ensureBlobUrl(layerId);
      if (url) {{
        fr.setAttribute('src', url);
      }} else {{
        // camada ausente
        fr.setAttribute('src', 'about:blank');
      }}
    }} finally {{
      showLoading(false);
    }}
  }}

  // menu
  document.querySelectorAll('.navbtn').forEach(b => {{
    b.addEventListener('click', () => activate(b.getAttribute('data-layer')));
  }});

  // abrir embutida
  document.getElementById('btnOpenEmbedded').addEventListener('click', async () => {{
    if (!current) return;
    const url = await ensureBlobUrl(current);
    if (!url) return;
    window.open(url, '_blank', 'noopener');
  }});

  // abrir original (arquivo do diretório, útil localmente)
  document.getElementById('btnOpenOriginal').addEventListener('click', () => {{
    if (!current) return;
    const meta = LAYERS[current];
    if (!meta || !meta.filename) return;
    // abre relativo ao healthcheck_full.html (mesmo diretório)
    window.open(meta.filename, '_blank', 'noopener');
  }});

  // missing
  if (missing && missing.length) {{
    const box = document.getElementById('missingBox');
    box.style.display = 'block';
    box.innerHTML = "<b>Atenção:</b> não encontrei alguns HTMLs de camada neste diretório:<br/>• " + missing.join("<br/>• ");
  }}

  // rota inicial
  let start = (location.hash || '').replace('#','') || 'layer0';
  if (!LAYERS[start]) start = 'layer0';

  // carrega inicial
  activate(start);

  // cleanup
  window.addEventListener('beforeunload', () => {{
    Object.values(blobUrls).forEach(u => {{
      try {{ URL.revokeObjectURL(u); }} catch(e) {{}}
    }});
  }});
}})();
</script>
</body>
</html>
"""

    out_path.write_text(hub_html, encoding="utf-8")

    print(f"[OK] relatório completo gerado: {out_path}")
    if missing:
        print("[WARN] camadas ausentes:")
        for fn in missing:
            print(f"  - {fn} (não encontrado em {html_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
