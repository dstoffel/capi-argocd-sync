FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY capi_argocd_sync.py .

USER 1000:1000

CMD ["python", "capi_argocd_sync.py"]
