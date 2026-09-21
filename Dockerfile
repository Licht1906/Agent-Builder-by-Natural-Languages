# ---- Stage 1: Build ----
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Stage 2: Production ----
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages into the system locations of the runtime image. The
# service runs as `appuser`, so keeping scripts under `/root/.local` makes the
# entrypoint unreadable and causes the container to restart forever.
COPY --from=builder /install /usr/local

# `pip install -r requirements.txt` ở trên cài dependency chứ không cài chính
# project (requirements.txt không có `-e .`), nên `src` chỉ import được khi
# /app nằm trên sys.path. uvicorn tự chèn cwd vào đó nên service chạy bình
# thường — nhưng `python scripts/x.py` thì không, vì Python đặt thư mục *của
# script* vào sys.path[0] chứ không phải cwd. Mọi Cloud Run Job chạy script
# (verify_uc1_data_surface, init_qdrant, seed_*) đều vấp đúng chỗ đó, và lỗi
# hiện ra là `ModuleNotFoundError: No module named 'src'`, nghe như thiếu
# dependency chứ không như sai sys.path.
ENV PYTHONPATH=/app

# Security: run as non-root user
RUN useradd -m appuser

# Copy application code
COPY . .

# Create data directory with correct ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "src.server_loop:new_event_loop"]
