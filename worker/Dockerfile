FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORK_DIR=/scratch

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN rm -rf /app/__pycache__

# Needs somewhere to write partial encodes. Mount a volume at /scratch, or
# give the container a tmpfs there if it has the memory to spare.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin forge \
    && mkdir -p /scratch && chown forge:forge /scratch
USER forge

CMD ["python", "-u", "agent.py"]
