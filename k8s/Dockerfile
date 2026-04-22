FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_k8s.py .
COPY mutation_guard.py .

ENV KUBECONFIG=/root/.kube/config

CMD ["python", "mcp_k8s.py"]
