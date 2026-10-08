ARG PYTHON_IMAGE=python:3.12
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app
COPY requirements.lock ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --no-cache-dir --index-url ${PIP_INDEX_URL} -r requirements.lock
COPY . .
EXPOSE 8000
CMD ["python", "scripts/demo.py", "serve", "--host", "0.0.0.0"]
