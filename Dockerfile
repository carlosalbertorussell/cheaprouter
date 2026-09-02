FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./

# MCPize injects PORT; default to 8081 for local runs
ENV PORT=8081
EXPOSE 8081

# Default to streamable-http transport (cloud). Use --stdio for local MCP clients.
CMD ["python", "server.py"]
