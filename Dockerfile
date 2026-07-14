FROM nvidia/cuda:12.9.0-cudnn-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Moscow

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 \
        python3.11-venv \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN pip install --upgrade pip

COPY requirements.txt ./
RUN pip install torch==2.12.0 torchvision==0.27.0 --index-url https://download.pytorch.org/whl/cu126 \
    && pip install -r requirements.txt

COPY src ./src
COPY data ./data
COPY prompts ./prompts
COPY ai-blueprint-to-ifc ./ai-blueprint-to-ifc

RUN mkdir -p /app/uploads /app/outputs

EXPOSE 6000

CMD ["gunicorn", "--bind", "0.0.0.0:6000", \
     "--workers", "1", "--threads", "4", \
     "--timeout", "3600", "--graceful-timeout", "60", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "src.wsgi:app"]
