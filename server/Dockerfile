FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Pre-bake fastembed model into image — eliminates 64MB HuggingFace download at container start.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed-cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
COPY server.py .
ENV MEM0_HISTORY_DB=/data/history.db
EXPOSE 8800
CMD ["python", "server.py"]
