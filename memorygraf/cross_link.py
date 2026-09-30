"""Enlazador cross-project por endpoints HTTP (DESIGN §6.2 aristas 'references').

La integración entre distintos proyectos ocurre por HTTP, no por imports. Este
módulo escanea literales de ruta ("/api/..." y URLs) en cada proyecto y une los
archivos de DISTINTOS proyectos que comparten la misma ruta -> arista references
(confidence < 1.0, provenance 'endpoint-match'). Así el grafo conecta los dos
proyectos como un solo sistema.
"""
from __future__ import annotations

import ipaddress
import os
import re
from collections import defaultdict
from urllib.parse import urlparse

from .model import Edge, Node, NODE_ENTITY
from .indexer import _iter_files, EXT_LANG

# Literales de ruta: '/api/...', "/bot/...", o URLs http(s)://host/path
RE_PATHLIT = re.compile(r"""['"`](https?://[^'"`]+|/[A-Za-z0-9][\w\-/]{2,})['"`]""")
_STATIC_EXT = (".css", ".js", ".png", ".svg", ".ico", ".map", ".jpg", ".woff", ".woff2")


def _clean_path(path: str) -> str:
    """Normaliza params dinámicos y barras finales."""
    path = re.sub(r":[A-Za-z_]\w*", ":p", path)   # /x/:id   -> /x/:p
    path = re.sub(r"\$\{[^}]+\}", ":p", path)       # /x/${id} -> /x/:p
    path = re.sub(r"\{[^}]+\}", ":p", path)          # /x/{id}  -> /x/:p
    return path.rstrip("/")


def _bad_brackets(raw: str) -> bool:
    """¿El netloc de `raw` lleva corchetes que NO encierran un IPv6 válido?

    Replica la validación que urlsplit hace desde CPython 3.11.4/3.12 (gh-103848), que
    no existe en todas las versiones soportadas: el 3.10.11 que instala setup-python en
    Windows —el último 3.10 con binarios para Windows— acepta
    `http://<SERVER>[:<PORT>]/x` sin quejarse y lo devuelve como ruta `/x`. Hacerla
    aquí da el mismo resultado en cualquier Python."""
    netloc = raw.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "[" not in netloc and "]" not in netloc:
        return False
    before, has_open, bracketed = netloc.rpartition("@")[2].partition("[")
    if not has_open or before:
        return True
    host, has_close, port = bracketed.partition("]")
    if not has_close or "[" in host or "]" in port or (port and not port.startswith(":")):
        return True
    if host.startswith("v"):                          # IPvFuture (RFC 3986)
        return re.fullmatch(r"v[0-9A-Fa-f]+\..+", host) is None
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return True
    return False


def _normalize(raw: str) -> str | None:
    """Devuelve un endpoint canónico, o None si no es un punto de integración útil.

    - URL con path:  http://host:3000/api/orders  -> /api/orders
    - URL sin path:  http://localhost:3000         -> host:localhost:3000
    - Ruta relativa: /api/orders/:id               -> /api/orders/:p (>=2 segmentos)
    """
    if raw.startswith(("http://", "https://")):
        try:
            if _bad_brackets(raw):
                raise ValueError("Invalid IPv6 URL")
            u = urlparse(raw)
        except ValueError:
            # Lo que entra aqui es texto raspado de archivos fuente, no una URL
            # verificada: basta con que EMPIECE por http:// para llegar. Como la
            # regex trata el backtick como delimitador (por los template literals
            # de JS), tambien captura el inline code de Markdown dentro de los
            # comentarios, donde abundan las plantillas de documentacion. Una de
            # ellas, `http://<SERVER>[:<PORT>]/<path>`, tumbaba el sync entero:
            # urlparse valida lo que va entre corchetes del netloc como IPv6 y
            # lanza ValueError("Invalid IPv6 URL") (en los Python que no lo hacen,
            # lo hace `_bad_brackets`). Un literal que ni siquiera se
            # puede parsear no es un punto de integracion: None, como el resto de
            # los descartes de esta funcion.
            return None
        path = _clean_path(u.path)
        segs = [s for s in path.split("/") if s]
        if not segs:                       # base URL sin ruta -> host de integración
            return f"host:{u.netloc}" if u.netloc else None
        if any(path.endswith(e) for e in _STATIC_EXT):
            return None
        return "/" + "/".join(segs)        # los endpoints de URL valen aunque sean 1 seg

    path = _clean_path(raw)
    if any(path.endswith(e) for e in _STATIC_EXT):
        return None
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:                       # rutas sueltas: exige >=2 segmentos (ruido)
        return None
    return "/" + "/".join(segs)


def link(store, config: dict) -> dict:
    excludes = set(config.get("excludes", []))
    from .indexer import DEFAULT_EXCLUDES
    excludes |= DEFAULT_EXCLUDES

    # path -> {project -> set(file_id)}
    hits: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for proj in config["projects"]:
        name, root = proj["name"], proj["root"]
        for abspath in _iter_files(root, excludes):
            relpath = os.path.relpath(abspath, root).replace("\\", "/")
            rel_id = f"{name}/{relpath}"
            try:
                with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue
            for m in RE_PATHLIT.finditer(source):
                norm = _normalize(m.group(1))
                if norm:
                    hits[norm][name].add(rel_id)

    edges_added, endpoints_shared, endpoint_nodes = 0, 0, 0
    for path, by_proj in hits.items():
        if len(by_proj) < 2:      # solo interesa lo que cruza proyectos
            continue
        endpoints_shared += 1
        # nodo entity para el endpoint compartido (concepto de dominio del sistema)
        eid = f"endpoint:{path}"
        if path.startswith("host:"):
            disp = path[len("host:"):]
            summary = f"Host de integración compartido entre proyectos: {disp}"
        else:
            disp = path
            summary = f"Endpoint HTTP compartido entre proyectos: {disp}"
        store.upsert_node(Node(
            id=eid, type=NODE_ENTITY, name=disp,
            summary=summary, tags=["endpoint", "integration"]))
        endpoint_nodes += 1
        projects = list(by_proj.keys())
        # conecta cada archivo al nodo endpoint (references) ...
        for name, files in by_proj.items():
            for fid in files:
                store.upsert_edge(Edge(source=fid, target=eid, type="references",
                                       confidence=0.7, provenance="endpoint-match"))
                edges_added += 1
        # ... y une directamente archivos de proyectos distintos (references)
        for i in range(len(projects)):
            for j in range(i + 1, len(projects)):
                for a in by_proj[projects[i]]:
                    for b in by_proj[projects[j]]:
                        store.upsert_edge(Edge(source=a, target=b, type="references",
                                               confidence=0.6,
                                               provenance="endpoint-match"))
                        edges_added += 1
    store.commit()
    return {"endpoints_shared": endpoints_shared,
            "cross_edges": edges_added,
            "endpoint_nodes": endpoint_nodes}
