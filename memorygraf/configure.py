"""Asistente interactivo de configuración (`memorygraf configure`).

Activa/ajusta las opciones opcionales de `.memorygraf/config.json` (las mismas de
`memorygraf.config.example.json`) de forma guiada y friendly, VALIDANDO que las
dependencias estén instaladas y orientando a `doctor`/`setup-ollama` si faltan.

Dos pantallas de entrada:
  1) Paquetes recomendados (por potencia): presets que activan un conjunto coherente.
  2) Opciones avanzadas: el usuario activa/desactiva cada opción a gusto.

Todo degrada con elegancia: activar algo sin su dependencia no rompe (se omite en
runtime); el asistente solo avisa y orienta.
"""
from __future__ import annotations

import json
import os


# --------------------------------------------------------------------------- #
# Dependencias (reusa los detectores de `doctor`)
# --------------------------------------------------------------------------- #
def _dep_ok(dep: str) -> bool:
    from . import doctor
    if dep == "ollama":
        ok, _ = doctor._has_ollama()
        return ok
    if dep == "lsp":
        return doctor._has_lsp() or doctor._has_pyright()
    return True


_DEP_LABEL = {"ollama": "Ollama (LLM local)", "lsp": "language server (pylsp/pyright)"}
_DEP_HINT = {
    "ollama": "memorygraf setup-ollama          # instala Ollama + un modelo local",
    "lsp": "memorygraf doctor --install lsp   # instala python-lsp-server (o pyright)",
}


# --------------------------------------------------------------------------- #
# Catálogo de opciones (cada una mapea a una clave de config.json)
# --------------------------------------------------------------------------- #
# path = (bloque, clave); on/off = valores; dep = dependencia requerida (o None).
FEATURES = [
    {
        "key": "summary_llm", "title": "Resúmenes con LLM local (Ollama)",
        "desc": ("Resúmenes en prosa real de cada símbolo con un modelo local y privado "
                 "(en vez del heurístico). Más ricos, pero el sync tarda más."),
        "path": ("summary", "backend"), "on": "ollama", "off": "heuristic", "dep": "ollama",
    },
    {
        "key": "compiler_llm", "title": "Narrativa/rerank con LLM local",
        "desc": ("El compilador de contexto narra el 'por qué' del co-cambio y reordena "
                 "búsquedas con el LLM local (fallback determinista si no está)."),
        "path": ("compiler", "backend"), "on": "ollama", "off": "auto", "dep": "ollama",
    },
    {
        "key": "resolver_llm", "title": "Desempate LLM de calls cross-file ambiguas (M9)",
        "desc": ("Cuando la resolución estática deja más de un candidato, el LLM local "
                 "elige el más probable (confianza baja + provenance 'llm', con fallback)."),
        "path": ("resolver", "llm"), "on": True, "off": False, "dep": "ollama",
    },
    {
        "key": "cochange_full", "title": "Co-cambio por historia completa (M1)",
        "desc": ("Capta el acoplamiento entre símbolos que el 'git blame' pierde (líneas "
                 "reescritas), re-extrayendo cada commit. Más lento (~14-23x); acotado."),
        "path": ("git", "symbol_cochange_full"), "on": True, "off": False, "dep": None,
    },
    {
        "key": "lsp_on_sync", "title": "Diagnósticos y tipos por símbolo en el sync (LSP)",
        "desc": ("Corre la capa LSP dentro del 'sync' (no solo en 'runtime --lsp'): tipos "
                 "resueltos y diagnósticos por símbolo. Requiere un language server."),
        "path": ("runtime", "lsp"), "on": True, "off": False, "dep": "lsp",
    },
    {
        "key": "local_vars", "title": "Tipos de variables locales (M4b)",
        "desc": ("Además de los parámetros, resuelve el tipo de las variables locales "
                 "(Python). Opt-in: son muchas y de valor marginal. Necesita la capa LSP."),
        "path": ("runtime", "local_var_types"), "on": True, "off": False, "dep": "lsp",
    },
]
_FEAT_BY_KEY = {f["key"]: f for f in FEATURES}

# Presets por potencia (pantalla de entrada, opción 1)
PRESETS = [
    {
        "name": "Portable (mínimo)",
        "desc": "Rápido, 100% offline, sin IA. Heurístico para todo. Cero dependencias extra.",
        "on": [],
    },
    {
        "name": "Estándar (recomendado)",
        "desc": ("Sin LLM (offline), pero grafo más rico: LSP en el sync (tipos + "
                 "diagnósticos) y co-cambio por historia completa."),
        "on": ["lsp_on_sync", "cochange_full"],
    },
    {
        "name": "Potencia (todo local)",
        "desc": ("Lo de Estándar + LLM LOCAL (Ollama) para resúmenes, compilador y desempate "
                 "de calls, + tipos de variables locales. Todo privado en tu máquina."),
        "on": ["lsp_on_sync", "cochange_full", "local_vars",
               "summary_llm", "compiler_llm", "resolver_llm"],
    },
]


# --------------------------------------------------------------------------- #
# Config I/O + estado de cada opción
# --------------------------------------------------------------------------- #
def _load(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _save(config_path: str, cfg: dict):
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _is_on(cfg: dict, feat: dict) -> bool:
    b, k = feat["path"]
    return (cfg.get(b) or {}).get(k) == feat["on"]


def _apply(cfg: dict, feat: dict, on: bool):
    b, k = feat["path"]
    cfg.setdefault(b, {})[k] = feat["on"] if on else feat["off"]


def _deps_needed(keys) -> list:
    """Dependencias (únicas) que requieren las features activadas, en orden estable."""
    seen, out = set(), []
    for key in keys:
        dep = _FEAT_BY_KEY[key].get("dep")
        if dep and dep not in seen:
            seen.add(dep)
            out.append(dep)
    return out


def _report_deps(enabled_keys, log) -> int:
    """Valida dependencias de las features activadas. Devuelve nº de dependencias faltantes."""
    deps = _deps_needed(enabled_keys)
    if not deps:
        return 0
    missing = [d for d in deps if not _dep_ok(d)]
    for d in deps:
        ok = _dep_ok(d)
        log(f"  dependencia {_DEP_LABEL[d]}: {'OK ✓' if ok else 'FALTA ✗'}")
        if not ok:
            log(f"    → {_DEP_HINT[d]}")
    if missing:
        log("  (las opciones quedan activas en la config; degradan hasta instalar la "
            "dependencia — nada se rompe.)")
    return len(missing)


# --------------------------------------------------------------------------- #
# Pantallas
# --------------------------------------------------------------------------- #
def _presets_screen(cfg, config_path, log, ask) -> int:
    log("\nPaquetes de configuración (por potencia):")
    for i, p in enumerate(PRESETS, 1):
        deps = _deps_needed(p["on"])
        deptxt = ("dependencias: " + ", ".join(_DEP_LABEL[d] for d in deps)) if deps else "sin dependencias extra"
        log(f"\n  {i}) {p['name']}")
        log(f"     {p['desc']}")
        log(f"     ({deptxt})")
    log("\n  0) Volver")
    choice = (ask("> ") or "").strip()
    if choice in ("", "0"):
        return -1
    if not (choice.isdigit() and 1 <= int(choice) <= len(PRESETS)):
        log("Opción inválida.")
        return -1
    preset = PRESETS[int(choice) - 1]
    on_keys = set(preset["on"])
    for feat in FEATURES:            # aplica TODAS (activa las del preset, apaga el resto)
        _apply(cfg, feat, feat["key"] in on_keys)
    _save(config_path, cfg)
    log(f"\n==> Config actualizada con el paquete '{preset['name']}': {config_path}")
    if preset["on"]:
        log("   Activadas: " + ", ".join(_FEAT_BY_KEY[k]["title"] for k in preset["on"]))
        log("   Validando dependencias:")
        _report_deps(preset["on"], log)
    else:
        log("   Todo en modo portable (heurístico/offline).")
    log("   Aplica con:  memorygraf sync")
    return 0


def _advanced_screen(cfg, config_path, log, ask) -> int:
    log("\nOpciones avanzadas — activa/desactiva cada una (Enter = dejar como está):")
    enabled_now = []
    for feat in FEATURES:
        cur = _is_on(cfg, feat)
        log(f"\n  [{'ON ' if cur else 'off'}] {feat['title']}")
        log(f"        {feat['desc']}")
        if feat.get("dep"):
            log(f"        depende de: {_DEP_LABEL[feat['dep']]}")
        ans = (ask("        ¿activar? (s/n, Enter=igual) > ") or "").strip().lower()
        if ans in ("s", "si", "sí", "y", "yes"):
            _apply(cfg, feat, True)
        elif ans in ("n", "no"):
            _apply(cfg, feat, False)
        if _is_on(cfg, feat):
            enabled_now.append(feat["key"])
    _save(config_path, cfg)
    log(f"\n==> Config actualizada: {config_path}")
    if "local_vars" in enabled_now and "lsp_on_sync" not in enabled_now:
        log("   Nota: 'tipos de variables locales' necesita la capa LSP. Actívala también "
            "en el sync ('LSP en el sync') o córrela con 'memorygraf runtime --lsp'.")
    if enabled_now:
        log("   Validando dependencias de lo activado:")
        _report_deps(enabled_now, log)
    log("   Aplica con:  memorygraf sync")
    return 0


def run(config_path: str | None, log=print, ask=input) -> int:
    """Asistente interactivo. Devuelve 0 si terminó ok (aunque el usuario salga)."""
    if not config_path or not os.path.exists(config_path):
        log("No hay un grafo MemoryGraf en este proyecto (falta .memorygraf/).")
        log("Inicialízalo primero:  memorygraf init")
        return 1
    cfg = _load(config_path)
    log("MemoryGraf · configuración")
    log("Activa las capacidades opcionales de tu grafo. Todo es local y degrada con")
    log("elegancia (si falta una dependencia, esa capacidad se omite; nada se rompe).")
    while True:
        log("\n  1) Paquetes recomendados (elige por potencia)")
        log("  2) Opciones avanzadas (activa cada una a gusto)")
        log("  0) Salir")
        try:
            choice = (ask("> ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            log("\nCancelado.")
            return 0
        if choice in ("", "0"):
            return 0
        if choice == "1":
            if _presets_screen(cfg, config_path, log, ask) == 0:
                return 0
        elif choice == "2":
            return _advanced_screen(cfg, config_path, log, ask)
        else:
            log("Opción inválida.")
