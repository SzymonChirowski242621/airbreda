FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ingest_air.py status.py broker.py ./
CMD ["python", "ingest_air.py"]
