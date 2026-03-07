FROM python:3.11-slim

RUN useradd -m -u 1000 icarus_user  
WORKDIR /app

COPY requirements.txt .  
RUN pip install --no-cache-dir -r requirements.txt

COPY . .  
RUN chown -R 1000:1000 /app

USER 1000  
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
