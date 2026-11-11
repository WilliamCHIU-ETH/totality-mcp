FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.lock /app/
RUN pip install --no-cache-dir -r requirements.lock
COPY common.py store.py server.py query_embed.py import_pg.py schema.sql /app/
RUN chmod -R a+rX /app
USER 10001:10001
ENTRYPOINT ["python", "/app/server.py"]
