FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /opt/ml/code

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SageMaker script mode / ScriptProcessor look for code under /opt/ml/code
ENV SAGEMAKER_PROGRAM=inference.py
