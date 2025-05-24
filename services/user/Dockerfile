FROM python:3.12-slim

WORKDIR /app

# Copy requirements.txt from project root
COPY requirements.txt .

# Copy the local service code
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]