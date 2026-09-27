FROM python:3.12-slim

# System deps for PDF parsing
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bank_ingest.py .
COPY expense_tracker/ expense_tracker/

# Create runtime directories (will be overridden by bind mounts)
RUN mkdir -p raw data backups reports

# Default: run the GUI
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "expense_tracker/gui.py", \
     "--server.port", "8501", \
     "--server.headless", "true", \
     "--server.address", "0.0.0.0", \
     "--browser.gatherUsageStats", "false"]
