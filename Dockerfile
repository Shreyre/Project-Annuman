# Anumaan demo: FastAPI + static UI in one container. Cloud Run sets $PORT.
#   docker build -t anumaan . && docker run -p 8080:8080 anumaan
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv
# versions tested locally
RUN pip install --no-cache-dir numpy==2.4.3 fastapi==0.135.1 uvicorn==0.41.0 pydantic==2.12.5 \
    google-genai==1.66.0 ortools==9.15.6755
COPY anumaan/ anumaan/
COPY app/ app/
COPY grammar/ grammar/
# the paper intake's SYNTHETIC sample pages (/samples); nothing else from tools/
COPY tools/samples/ tools/samples/
USER nobody
CMD exec uvicorn app.main:app --host 0.0.0.0 --port $PORT
