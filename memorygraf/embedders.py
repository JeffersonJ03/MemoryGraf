"""Embedders de MemoryGraf (DESIGN §3.8, §11 — Fase 3).

Los vectores son CACHÉ REGENERABLE, nunca fuente de verdad. El embedder es
intercambiable. Por defecto usa un embedder LOCAL sin dependencias (offline, el
código nunca sale de la máquina). Opcionalmente, un embedder de API neuronal si
el usuario lo activa conscientemente (envía texto a un servicio externo).

Vectores dispersos: dict[str, float], L2-normalizados. La cosine es el producto
punto (por estar normalizados).
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict

# --- Tokenización de identificadores (la clave del recall) ---
_NONWORD = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# stopwords mínimas ES/EN + ruido de código
_STOP = {
    "the", "and", "for", "with", "que", "los", "las", "del", "por", "una", "uno",
    "de", "la", "el", "en", "un", "self", "this", "const", "let", "var", "def",
    "function", "class", "import", "from", "return", "async", "await", "true",
    "false", "none", "null", "src",
}


def tokenize(text: str) -> list[str]:
    """Divide en tokens, separando camelCase y snake/kebab, en minúsculas.

    'failureAnalyticsModel' -> ['failure','analytics','model','failureanalyticsmodel']
    """
    out: list[str] = []
    for raw in _NONWORD.split(text or ""):
        if not raw:
            continue
        for part in _CAMEL.sub(" ", raw).split():
            p = part.lower()
            if len(p) >= 2 and p not in _STOP and not p.isdigit():
                out.append(p)
        low = raw.lower()
        if len(low) >= 3 and low not in _STOP and low not in out and not low.isdigit():
            out.append(low)  # también el identificador completo
    return out


def cosine(a: dict, b: dict) -> float:
    """Producto punto de dos vectores dispersos ya L2-normalizados = cosine."""
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(k, 0.0) for k, w in a.items())


def _l2_normalize(vec: dict) -> dict:
    norm = math.sqrt(sum(w * w for w in vec.values()))
    if norm == 0:
        return vec
    return {k: w / norm for k, w in vec.items()}


class Embedder:
    """Interfaz. Un embedder mapea texto -> vector disperso normalizado."""
    name = "base"

    def fit(self, documents: list[str]) -> None:
        """Opcional: aprende estadísticas del corpus (p.ej. IDF)."""

    def embed_one(self, text: str) -> dict:
        raise NotImplementedError

    def to_meta(self) -> str:
        return "{}"

    def load_meta(self, meta: str) -> None:
        pass


class LocalTfidfEmbedder(Embedder):
    """TF-IDF local, sin dependencias, offline. Vector disperso por token.

    Bueno para consultas multi-término y morfología de identificadores. NO
    generaliza sinónimos entre idiomas (eso requiere un embedder neuronal, que
    esta arquitectura permite enchufar). Honesto: es estadístico, no neuronal.
    """
    name = "local-tfidf-v1"

    def __init__(self):
        self.idf: dict[str, float] = {}
        self.n_docs: int = 0
        self._default_idf: float = 1.0

    def fit(self, documents: list[str]) -> None:
        df: dict[str, int] = defaultdict(int)
        self.n_docs = len(documents)
        for doc in documents:
            for tok in set(tokenize(doc)):
                df[tok] += 1
        self.idf = {t: math.log((self.n_docs + 1) / (d + 1)) + 1.0
                    for t, d in df.items()}
        # término desconocido (solo en la consulta): IDF alto -> discrimina
        self._default_idf = math.log((self.n_docs + 1) / 1) + 1.0

    def embed_one(self, text: str) -> dict:
        tf = Counter(tokenize(text))
        vec = {t: c * self.idf.get(t, self._default_idf) for t, c in tf.items()}
        return _l2_normalize(vec)

    def to_meta(self) -> str:
        return json.dumps({"idf": self.idf, "n_docs": self.n_docs,
                           "default_idf": self._default_idf}, ensure_ascii=False)

    def load_meta(self, meta: str) -> None:
        d = json.loads(meta)
        self.idf = d["idf"]
        self.n_docs = d["n_docs"]
        self._default_idf = d["default_idf"]


class ApiEmbedder(Embedder):
    """Embedder neuronal vía API compatible OpenAI (OPT-IN, envía texto fuera).

    Se activa solo si MEMORYGRAF_EMBED_URL y MEMORYGRAF_EMBED_KEY están definidos.
    Usa urllib (sin dependencias). Devuelve el denso como vector disperso
    (dict índice->valor) L2-normalizado, para unificar el ranking.
    """
    def __init__(self, url: str, key: str, model: str):
        self.url = url
        self.key = key
        self.model = model
        self.name = f"api:{model}"

    def embed_one(self, text: str) -> dict:
        import urllib.request
        payload = json.dumps({"model": self.model, "input": text}).encode()
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        emb = data["data"][0]["embedding"]
        return _l2_normalize({str(i): v for i, v in enumerate(emb) if v})


class NeuralEmbedder(Embedder):
    """Embedder neuronal local vía model2vec (estático, rápido en CPU, multilingüe).

    Da semántica REAL cross-idioma (p.ej. 'order' ~ 'orden') sin API ni GPU. Opcional:
    si model2vec no está instalado, get_embedder cae al TF-IDF local. El modelo se
    descarga una vez y luego funciona offline.
    """
    def __init__(self, model_name: str):
        from model2vec import StaticModel
        self.model = StaticModel.from_pretrained(model_name)
        self.name = f"neural:{model_name.split('/')[-1]}"

    def embed_one(self, text: str) -> dict:
        vec = self.model.encode([text or ""])[0]
        return _l2_normalize({str(i): float(v) for i, v in enumerate(vec) if v})


def get_embedder(config: dict | None = None) -> Embedder:
    """Selecciona el embedder con degradación elegante.

    Prioridad: forzado por env -> API (si configurada) -> neuronal (model2vec) -> TF-IDF.
    """
    forced = os.environ.get("MEMORYGRAF_EMBEDDER", "").lower()  # tfidf|neural|api
    url = os.environ.get("MEMORYGRAF_EMBED_URL")
    key = os.environ.get("MEMORYGRAF_EMBED_KEY")
    model = os.environ.get("MEMORYGRAF_EMBED_MODEL", "text-embedding-3-small")
    m2v = os.environ.get("MEMORYGRAF_M2V_MODEL", "minishlab/potion-multilingual-128M")

    if forced == "tfidf":
        return LocalTfidfEmbedder()
    if (forced == "api" or (not forced and url and key)) and url and key:
        return ApiEmbedder(url, key, model)
    if forced in ("", "neural"):
        try:
            return NeuralEmbedder(m2v)
        except Exception:
            if forced == "neural":
                raise
    return LocalTfidfEmbedder()
