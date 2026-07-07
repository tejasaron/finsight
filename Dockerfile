# FinSight — containerized for Cloud Run deployment.
# Build: docker build -t finsight .
# Run locally: docker run -p 8080:8080 --env-file .env finsight
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# `adk api_server` serves the agent(s) in this directory over HTTP, which is
# what Cloud Run expects (a container listening on $PORT).
CMD ["sh", "-c", "adk api_server --host 0.0.0.0 --port ${PORT} ."]
