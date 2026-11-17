FROM python:3.12-slim

WORKDIR /service
COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY examples ./examples
COPY alembic ./alembic
# Optional cloud trust bundle is not persisted in the image or build arguments.
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then \
      PIP_CERT=/run/secrets/build_ca pip install --no-cache-dir .; \
    else pip install --no-cache-dir .; fi

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
