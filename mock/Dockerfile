FROM python:3.12-slim
WORKDIR /srv/mock
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mock ./mock
CMD ["uvicorn", "mock.main:app", "--host", "0.0.0.0", "--port", "8001"]
