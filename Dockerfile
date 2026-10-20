# MemoryGraf en contenedor. Uso típico, montando el proyecto en /workspace:
#
#   docker build -t memorygraf .
#   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" memorygraf init
#   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" memorygraf sync
#   docker run --rm -i --user "$(id -u):$(id -g)" -v "$PWD:/workspace" memorygraf mcp   # stdio (-i obligatorio)
#
# --user: en Linux el bind mount conserva el uid del host, y `appuser` (10001) no
# puede crear /workspace/.memorygraf ("Permission denied" en `init`). Corriendo con
# tu uid escribe y lo creado queda tuyo. En Docker Desktop (Windows/macOS) sobra,
# pero no estorba.
#
# EXTRAS por defecto = potencia SIN `neural`: model2vec bajaría ~480 MB desde
# HuggingFace en cada contenedor efímero (la caché vive en $HOME, que no persiste).
# Para incluirlo: --build-arg EXTRAS=full y monta un volumen en /home/appuser/.cache.

FROM python:3.12-slim-bookworm AS builder
ARG EXTRAS=parsers,watch,lsp
WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# build-essential: red de seguridad por si algún extra no trae wheel para la plataforma.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY memorygraf ./memorygraf
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[${EXTRAS}]"


FROM python:3.12-slim-bookworm AS runtime
# HOME fijo: con --user <uid> sin entrada en /etc/passwd, HOME sería "/" y las
# cachés (~/.cache de model2vec, etc.) fallarían o esquivarían el volumen documentado.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/appuser
# git: la capa temporal (git_layer) invoca el binario; sin él queda vacía en silencio.
# safe.directory: el volumen montado pertenece a otro uid y git lo rechazaría
# como "dubious ownership".
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*' \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chmod 1777 /home/appuser   # escribible por cualquier --user (como /tmp)
# El paquete ya está instalado dentro del venv; no hace falta copiar el código fuente.
# El venv queda de root (solo lectura para appuser).
COPY --from=builder /opt/venv /opt/venv
USER appuser
WORKDIR /workspace
ENTRYPOINT ["memorygraf"]
CMD ["--help"]
