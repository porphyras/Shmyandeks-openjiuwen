# Harness image: python + bash, openJiuwen pinned, no network access needed at run time.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/mpl \
    OPENJIUWEN_TOKENIZER_CACHE_DIR=/tmp/oj_tok

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && cd /tmp && python -c "import openjiuwen" && rm -rf /tmp/* /root/.cache

COPY run_agent.py /app/run_agent.py
COPY harness /app/harness
RUN python -m compileall -q /app/harness /app/run_agent.py

ENTRYPOINT ["python", "/app/run_agent.py"]
CMD ["--tasks-dir", "/data/tasks"]
