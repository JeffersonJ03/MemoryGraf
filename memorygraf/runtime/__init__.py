"""CAPA 2 · Verdad de runtime (PLAN-CAPAS-CONTEXTUALES §5).

Pasa de "lo que el código *parece* según el texto" a "lo que el código *es y hace*
según las herramientas que ya corren en la máquina": tests + cobertura (sub-capa B,
`tests.py`) y tipos/diagnósticos vía LSP (sub-capa A, `lsp.py`).

Todo es CACHÉ REGENERABLE desde artefactos (coverage.xml, JUnit, LSP), nunca fuente de
verdad (DESIGN §3.8). Cada sub-capa es independiente y degrada sola: sin artefactos de
cobertura o sin language-server instalado, se omite; el resto del grafo queda intacto.
"""
