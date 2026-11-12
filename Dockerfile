# Harness image: python + bash, openJiuwen pinned, no network access needed at run time.
# The *_OFFLINE / telemetry switches keep SDK code paths the harness never calls (model hubs,
# tracing exporters) from reaching out even if one were triggered; only LLM_BASE_URL is used.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/mpl \
    OPENJIUWEN_TOKENIZER_CACHE_DIR=/tmp/oj_tok \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    OTEL_SDK_DISABLED=true \
    OTEL_TRACES_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none \
    DO_NOT_TRACK=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    TOKENIZERS_PARALLELISM=false \
    RAYON_NUM_THREADS=2

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && cd /tmp && python -c "import openjiuwen, onnxruntime, tokenizers" && rm -rf /tmp/* /root/.cache

COPY run_agent.py /app/run_agent.py
COPY harness /app/harness
COPY models /app/models
RUN python -m compileall -q /app/harness /app/run_agent.py

ENTRYPOINT ["python", "/app/run_agent.py"]
CMD ["--tasks-dir", "/data/tasks"]
