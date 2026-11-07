# One image for every workload (discovery, worker, render, sweeper, console); the command picks the role.
# Pin the base by digest in CI (docker buildx imagetools inspect python:3.12-slim-bookworm).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import imageio_ffmpeg, os; p = imageio_ffmpeg.get_ffmpeg_exe(); os.chmod(p, 0o755); print(p)"
COPY src ./src

# Non-root, no shell login, nothing writable except /tmp.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
ENV BACKEND=azure HOME=/tmp
EXPOSE 8000
ENTRYPOINT ["python", "-m", "ytp"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
