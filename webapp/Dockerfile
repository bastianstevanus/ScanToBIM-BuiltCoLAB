FROM python:3.11-slim

WORKDIR /app

# System deps required by open3d and ifcopenshell
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1-mesa-glx \
        libglib2.0-0 \
        libgomp1 \
        libsm6 \
        libxext6 \
        libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads outputs

EXPOSE 8000

ENV DOCKER_ENV=1

CMD ["python", "start.py"]
