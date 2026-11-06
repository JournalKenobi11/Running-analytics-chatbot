FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck and fontconfig for matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create output folder
RUN mkdir -p /app/output

COPY dreeve_engine.py .
COPY app.py .

# Streamlit configurations
ENV STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYTHONUNBUFFERED=1

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "app.py"]
