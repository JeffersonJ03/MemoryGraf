"""Visualización del grafo (DESIGN §9 — extra). Genera un HTML/SVG autocontenido.

Muestra "lo que ve la IA": nodos (archivos/entidades) y sus conexiones
(imports/calls/references/models). El layout se precalcula en Python
(Fruchterman-Reingold) y se emite como SVG estático + JS mínimo de pan/zoom.

Por qué SVG y no canvas: renderiza SIEMPRE (con o sin JS, en cualquier iframe) y no
depende de embeber datos en un <script> (evita que un `</script>` en un resumen rompa
la página). Todo el texto va XML-escapado. Sin dependencias ni CDN (CSP-safe).
"""
from __future__ import annotations

import math

_PALETTE = ["#4f9dff", "#ff8c42", "#3ecf8e", "#e5576f", "#b980f0",
            "#f2c14e", "#2dd4bf", "#f472b6", "#a3e635", "#38bdf8"]
_EDGE_COLOR = {"imports": "#3ecf8e", "calls": "#4f9dff", "references": "#e5576f",
               "models": "#f2c14e", "implements": "#b980f0", "depends_on": "#6e7681",
               "defines": "#30363d", "governs": "#ff8c42", "relates_to": "#484f58"}
_VW, _VH = 1600, 1000


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _file_of(node: dict) -> str:
    if node["type"] == "symbol":
        return node.get("path") or node["id"]
    return node["id"]


def build_graph_data(store, level="file", scope=None, max_nodes=400,
                     include_external=False) -> dict:
    all_nodes = {n["id"]: n for n in store.all_nodes()}
    keep_types = {"file", "entity"} | ({"external"} if include_external else set())
    if level == "symbol":
        keep_types |= {"symbol"}
    cand = {}
    for nid, n in all_nodes.items():
        if n["type"] not in keep_types:
            continue
        if scope and scope not in (n.get("path") or n["id"]):
            continue
        cand[nid] = n

    agg, deg = {}, {}
    for e in store.all_edges():
        sn, tn = all_nodes.get(e["source"]), all_nodes.get(e["target"])
        if not sn or not tn:
            continue
        s, t = (_file_of(sn), _file_of(tn)) if level == "file" else (e["source"], e["target"])
        if s == t or s not in cand or t not in cand:
            continue
        key = (s, t, e["type"])
        agg[key] = agg.get(key, 0) + 1
        deg[s] = deg.get(s, 0) + 1
        deg[t] = deg.get(t, 0) + 1

    keep = set(sorted(cand, key=lambda i: deg.get(i, 0), reverse=True)[:max_nodes])

    def label(n):
        return (n.get("path") or n["id"]).split("/")[-1] if n["type"] == "file" else n["name"]

    def proj(n):
        return n.get("project") or ("(dominio)" if n["type"] == "entity" else "(ext)")

    projects = sorted({proj(cand[i]) for i in keep})
    nodes = [{"id": i, "label": label(cand[i]), "proj": proj(cand[i]),
              "type": cand[i]["type"], "deg": deg.get(i, 0),
              "path": cand[i].get("path") or "",
              "summary": (cand[i].get("summary") or "")[:160]} for i in keep]
    links = [{"s": s, "t": t, "type": ty, "w": w}
             for (s, t, ty), w in agg.items() if s in keep and t in keep]
    return {"nodes": nodes, "links": links, "projects": projects, "level": level}


def _radius(deg):
    return 4 + math.sqrt(deg or 1) * 1.7


def _layout(nodes, links, iters=420):
    """Fruchterman-Reingold + gravedad, y una pasada anti-colisión al final.

    La gravedad evita que los nodos sueltos vuelen lejos (lo que aplastaría el resto al
    normalizar); la anti-colisión garantiza que ningún nodo quede encima de otro.
    """
    n = len(nodes)
    if n == 0:
        return {}
    idx = {nd["id"]: i for i, nd in enumerate(nodes)}
    px = [0.0] * n
    py = [0.0] * n
    for i in range(n):                      # init determinista en espiral áurea
        ang = i * 2.399963
        rad = 24 * math.sqrt(i + 1)
        px[i] = math.cos(ang) * rad
        py[i] = math.sin(ang) * rad
    edges = [(idx[l["s"]], idx[l["t"]]) for l in links if l["s"] in idx and l["t"] in idx]
    k = 1.2 * math.sqrt((_VW * _VH) / n)     # distancia ideal (mayor = más disperso)
    grav = 0.02                               # gravedad suave (evita colapsar el centro)
    t = _VW / 6.0
    for _ in range(iters):
        dx = [0.0] * n
        dy = [0.0] * n
        for i in range(n):
            xi, yi = px[i], py[i]
            for j in range(i + 1, n):
                ddx = xi - px[j]
                ddy = yi - py[j]
                d2 = ddx * ddx + ddy * ddy + 0.01
                d = math.sqrt(d2)
                f = k * k / d               # repulsión
                ux, uy = ddx / d, ddy / d
                dx[i] += ux * f; dy[i] += uy * f
                dx[j] -= ux * f; dy[j] -= uy * f
        for a, b in edges:                  # atracción por arista
            ddx = px[a] - px[b]
            ddy = py[a] - py[b]
            d = math.sqrt(ddx * ddx + ddy * ddy) + 0.01
            f = d * d / k
            ux, uy = ddx / d, ddy / d
            dx[a] -= ux * f; dy[a] -= uy * f
            dx[b] += ux * f; dy[b] += uy * f
        for i in range(n):                  # gravedad hacia el centro + paso limitado
            dx[i] -= px[i] * grav
            dy[i] -= py[i] * grav
            dl = math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) + 1e-9
            px[i] += dx[i] / dl * min(dl, t)
            py[i] += dy[i] / dl * min(dl, t)
        t = max(2.0, t * 0.985)

    # normaliza al viewBox con margen
    pad = 70
    mnx, mxx, mny, mxy = min(px), max(px), min(py), max(py)
    s = min((_VW - 2 * pad) / (mxx - mnx or 1), (_VH - 2 * pad) / (mxy - mny or 1))
    ox = (_VW - (mxx - mnx) * s) / 2
    oy = (_VH - (mxy - mny) * s) / 2
    X = [ox + (px[i] - mnx) * s for i in range(n)]
    Y = [oy + (py[i] - mny) * s for i in range(n)]
    R = [_radius(nodes[i]["deg"]) for i in range(n)]

    # anti-colisión + espaciado mínimo: separa pares próximos (expande el núcleo denso)
    for _ in range(200):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                ddx = X[i] - X[j]
                ddy = Y[i] - Y[j]
                d = math.sqrt(ddx * ddx + ddy * ddy) + 1e-6
                need = R[i] + R[j] + 20
                if d < need:
                    push = (need - d) / 2
                    ux, uy = ddx / d, ddy / d
                    X[i] += ux * push; Y[i] += uy * push
                    X[j] -= ux * push; Y[j] -= uy * push
                    moved = True
        if not moved:
            break
    for i in range(n):                      # mantén dentro del viewBox
        X[i] = min(_VW - pad, max(pad, X[i]))
        Y[i] = min(_VH - pad, max(pad, Y[i]))
    return {nodes[i]["id"]: (X[i], Y[i]) for i in range(n)}


def build_html(store, level="file", scope=None, max_nodes=400,
               include_external=False, fragment=False) -> str:
    data = build_graph_data(store, level, scope, max_nodes, include_external)
    pos = _layout(data["nodes"], data["links"])
    proj_color = {p: _PALETTE[i % len(_PALETTE)] for i, p in enumerate(data["projects"])}

    def color(nd):
        if nd["type"] == "entity":
            return "#f2c14e"
        if nd["type"] == "external":
            return "#6e7681"
        return proj_color.get(nd["proj"], "#4f9dff")

    # aristas
    edge_svg = []
    for l in data["links"]:
        if l["s"] not in pos or l["t"] not in pos:
            continue
        x1, y1 = pos[l["s"]]; x2, y2 = pos[l["t"]]
        w = min(3.0, 0.5 + l["w"] * 0.3)
        edge_svg.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{_EDGE_COLOR.get(l["type"], "#30363d")}" stroke-width="{w:.2f}" '
            f'stroke-opacity="0.35"/>')

    # nodos + etiquetas (etiqueta solo los más conectados para no saturar)
    degs = sorted((nd["deg"] for nd in data["nodes"]), reverse=True)
    label_thresh = degs[min(len(degs) - 1, 34)] if degs else 0
    node_svg, label_svg = [], []
    for nd in data["nodes"]:
        if nd["id"] not in pos:
            continue
        x, y = pos[nd["id"]]
        r = 4 + math.sqrt(nd["deg"] or 1) * 1.7
        tip = f'{nd["label"]}  [{nd["type"]}] · {nd["proj"]} · {nd["deg"]} conexiones'
        if nd["summary"]:
            tip += "\n" + nd["summary"]
        if nd["path"]:
            tip += "\n" + nd["path"]
        node_svg.append(
            f'<circle class="n" data-l="{_esc(nd["label"].lower())}" '
            f'cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{color(nd)}">'
            f'<title>{_esc(tip)}</title></circle>')
        if nd["deg"] >= label_thresh and nd["deg"] > 1:
            label_svg.append(
                f'<text x="{x + r + 2:.1f}" y="{y + 3:.1f}" font-size="12" '
                f'fill="#c9d1d9">{_esc(nd["label"])}</text>')

    legend = ('<b>Proyectos:</b> ' + ' '.join(
        f'<span class="dot" style="background:{proj_color.get(p, "#888")}"></span>{_esc(p)}'
        for p in data["projects"])
        + '<span class="dot" style="background:#f2c14e"></span>entidad'
        + '<br><b>Conexiones:</b> '
        + '<span class="dot" style="background:#4f9dff"></span>calls'
        + '<span class="dot" style="background:#3ecf8e"></span>imports'
        + '<span class="dot" style="background:#e5576f"></span>references'
        + '<span class="dot" style="background:#f2c14e"></span>models'
        + '<span class="dot" style="background:#b980f0"></span>implements')

    sub = f'{data["level"]} · {len(data["nodes"])} nodos · {len(data["links"])} conexiones'
    body = (_BODY
            .replace("__SUB__", _esc(sub))
            .replace("__EDGES__", "".join(edge_svg))
            .replace("__NODES__", "".join(node_svg))
            .replace("__LABELS__", "".join(label_svg))
            .replace("__LEGEND__", legend)
            .replace("__VW__", str(_VW)).replace("__VH__", str(_VH)))
    if fragment:
        return body
    return ("<!doctype html><html lang=es><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>MemoryGraf — grafo</title></head><body>" + body + "</body></html>")


_BODY = r"""
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  .mgwrap{position:relative;width:100%;height:100vh;min-height:560px;margin:0;
    background:#0d1117;color:#e6edf3;font:14px/1.4 system-ui,sans-serif;overflow:hidden}
  .mghud{position:absolute;top:0;left:0;right:0;z-index:3;padding:10px 14px;display:flex;
    gap:12px;align-items:center;flex-wrap:wrap;
    background:linear-gradient(#0d1117,rgba(13,17,23,.55),transparent)}
  .mghud h1{font-size:14px;margin:0;font-weight:600}
  .mghud .muted{color:#8b949e;font-size:12px}
  .mghud input{background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:5px 9px;width:180px}
  .mghud button{background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:5px 10px;cursor:pointer}
  .mghud button:hover{border-color:#4f9dff}
  .mglegend{position:absolute;bottom:10px;left:14px;z-index:3;font-size:12px;color:#8b949e;
    background:rgba(13,17,23,.72);padding:8px 10px;border-radius:8px;max-width:60vw}
  .mglegend b{color:#e6edf3}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 10px;vertical-align:middle}
  #mgsvg{display:block;width:100%;height:100%;cursor:grab}
  #mgsvg text{pointer-events:none;paint-order:stroke;stroke:#0d1117;stroke-width:3px;font-family:system-ui}
  circle.n{cursor:pointer}
  circle.n:hover{stroke:#fff;stroke-width:2}
  .dim{opacity:.12}
</style>
<div class="mgwrap">
  <div class="mghud">
    <h1>🧠 MemoryGraf</h1>
    <span class="muted">__SUB__</span>
    <input id="mgq" placeholder="resaltar por nombre…">
    <button id="mgreset">⟲ centrar</button>
    <span class="muted">pasa el cursor para ver detalle · rueda=zoom · arrastra=mover</span>
  </div>
  <div class="mglegend">__LEGEND__</div>
  <svg id="mgsvg" viewBox="0 0 __VW__ __VH__" preserveAspectRatio="xMidYMid meet">
    <g id="mgvp">
      <g stroke-linecap="round">__EDGES__</g>
      <g>__NODES__</g>
      <g>__LABELS__</g>
    </g>
  </svg>
</div>
<script>
(function(){
  var svg=document.getElementById('mgsvg'), vp=document.getElementById('mgvp');
  var VW=__VW__, VH=__VH__, k=1, tx=0, ty=0, drag=null;
  function apply(){vp.setAttribute('transform','translate('+tx+' '+ty+') scale('+k+')');}
  function pt(e){var r=svg.getBoundingClientRect();return {x:(e.clientX-r.left)/r.width*VW,y:(e.clientY-r.top)/r.height*VH};}
  svg.addEventListener('wheel',function(e){e.preventDefault();var p=pt(e),f=e.deltaY<0?1.15:0.87;
    var nk=Math.max(0.3,Math.min(8,k*f));tx=p.x-(p.x-tx)*(nk/k);ty=p.y-(p.y-ty)*(nk/k);k=nk;apply();},{passive:false});
  svg.addEventListener('mousedown',function(e){drag={x:e.clientX,y:e.clientY,tx:tx,ty:ty};svg.style.cursor='grabbing';});
  window.addEventListener('mousemove',function(e){if(!drag)return;var r=svg.getBoundingClientRect();
    tx=drag.tx+(e.clientX-drag.x)/r.width*VW;ty=drag.ty+(e.clientY-drag.y)/r.height*VH;apply();});
  window.addEventListener('mouseup',function(){drag=null;svg.style.cursor='grab';});
  document.getElementById('mgreset').onclick=function(){k=1;tx=0;ty=0;apply();};
  var q=document.getElementById('mgq'), circles=svg.querySelectorAll('circle.n');
  q.addEventListener('input',function(){var s=q.value.trim().toLowerCase();
    circles.forEach(function(c){var m=!s||c.getAttribute('data-l').indexOf(s)>=0;
      c.classList.toggle('dim',!m);});});
})();
</script>
"""
