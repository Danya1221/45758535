FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catalog_bot ./catalog_bot
COPY examples ./examples
CMD ["python", "-m", "catalog_bot", "run"]
