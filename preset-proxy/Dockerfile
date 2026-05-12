FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static

ENV PYTHONUNBUFFERED=1
EXPOSE 8788

CMD ["python", "/app/app.py"]
