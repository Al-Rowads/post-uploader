FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /data && chown 101:101 /data
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY post_uploader ./post_uploader
USER 101:101
CMD ["python", "-m", "post_uploader", "run"]
