FROM docker.io/library/python:3.13-slim
RUN useradd -u 1000 -m steam \
 && install -d -o steam -g steam /data/klei/DoNotStarveTogether /data/backups /data/parked /ctl /app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dstsaves ./dstsaves
USER steam
EXPOSE 8081
CMD ["uvicorn", "dstsaves.main:app", "--host", "0.0.0.0", "--port", "8081", "--log-level", "warning"]
