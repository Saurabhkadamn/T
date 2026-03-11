FROM python:3.11-slim

WORKDIR /app

# Create non-root user before any COPY so --chown works without a separate chown layer
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Install deps as root (wheels go to /usr/local/lib — outside /app)
# This layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code with correct ownership in a single layer (avoids duplicate chown layer)
COPY --chown=appuser:appgroup . .

USER appuser

EXPOSE 8000

# Default: run the FastAPI server
# Worker override (EKS job spec): ["python", "-m", "worker.main"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
