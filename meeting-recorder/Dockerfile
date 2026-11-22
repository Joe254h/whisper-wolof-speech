FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PORT=8000 DATA_DIR=/home/user/app/data
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .

EXPOSE 8000
# One worker keeps job state in one process; threads serve uploads while a job runs.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 900 app:app"]
