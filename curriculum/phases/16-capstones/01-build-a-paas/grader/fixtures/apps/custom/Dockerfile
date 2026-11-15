FROM python:3.13-alpine
LABEL cc.fixture="custom"
WORKDIR /srv/custom
COPY app.py body.txt health.txt ./
ENV PORT=8080
CMD ["python3", "app.py"]
