FROM python:3.12-slim

WORKDIR /service
COPY pyproject.toml README.md ./
COPY app ./app
# Some managed build environments inject an HTTPS proxy. Its CA is supplied at
# build time only; ordinary local Docker builds leave this argument empty.
ARG ENVIRONMENT_PROXY_CA=""
RUN if [ -n "$ENVIRONMENT_PROXY_CA" ]; then \
      printf '%s\n' "$ENVIRONMENT_PROXY_CA" > /usr/local/share/ca-certificates/environment-proxy-ca.crt && \
      update-ca-certificates; \
    fi && \
    pip install --no-cache-dir .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
