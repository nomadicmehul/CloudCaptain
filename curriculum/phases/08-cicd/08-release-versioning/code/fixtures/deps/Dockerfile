ARG PYTHON_VERSION=3.12
FROM python:3.12-slim AS build
RUN pip install --no-cache-dir -r requirements.txt

FROM build AS test
RUN python -m unittest

FROM python:${PYTHON_VERSION}-alpine AS lint

FROM nginx:1.27.4-alpine@sha256:1111111111111111111111111111111111111111111111111111111111111111 AS web

FROM busybox AS debug

FROM scratch
COPY --from=build /app /app
