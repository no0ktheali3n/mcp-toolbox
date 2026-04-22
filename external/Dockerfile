FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_external.py .

EXPOSE 8002

CMD ["python", "mcp_external.py"]
