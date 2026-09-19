FROM python:3.11-slim

# تثبيت أدوات البناء اللازمة لتجميع حزمة quickjs في بيئة لينكس
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# المنفذ الافتراضي (يقوم Render بتمرير المنفذ تلقائياً عبر متغير البيئة PORT)
ENV PORT=8001
EXPOSE 8001

CMD ["python", "app.py"]
