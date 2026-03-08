FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Default: run the FastAPI server
# Worker override (EKS job spec): ["python", "-m", "worker.main"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
