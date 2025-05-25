FROM python:3.12-slim

WORKDIR /app

# Install psql
RUN apt-get update && apt-get install -y postgresql-client

COPY requirements.txt .
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

COPY wait-for-postgres.sh .
RUN chmod +x wait-for-postgres.sh

EXPOSE 8001

CMD ["./wait-for-postgres.sh", "postgres", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
