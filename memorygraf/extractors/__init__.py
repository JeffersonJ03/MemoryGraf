"""Extractores por lenguaje. Cada uno devuelve (nodes, edges, raw_imports).

- Python: AST de stdlib -> alta fidelidad (confidence 1.0).
- JS/TS/TSX: heurística por regex -> confidence < 1.0, provenance 'regex' (DESIGN §6.3).
"""
