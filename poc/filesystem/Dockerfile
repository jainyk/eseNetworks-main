FROM python:3.12-slim AS payload
ARG PAYLOAD_MIB=256
ARG PAYLOAD_SEED=1
RUN python -c "import os,random; n=int(os.environ.get('PAYLOAD_MIB','256'))*1024*1024; r=random.Random(int(os.environ.get('PAYLOAD_SEED','1'))); f=open('/payload.bin','wb'); [f.write(r.randbytes(min(1024*1024,n-i))) for i in range(0,n,1024*1024)]; f.close()"

FROM python:3.12-slim AS origin
WORKDIR /app
COPY origin.py /app/origin.py
COPY --from=payload /payload.bin /data/payload.bin
EXPOSE 8081
CMD ["python", "/app/origin.py", "--file", "/data/payload.bin", "--port", "8081"]

FROM python:3.12-slim AS workload-base
WORKDIR /app
COPY workload.py /app/workload.py
EXPOSE 8080

FROM workload-base AS baseline
COPY --from=payload /payload.bin /app/payload.bin
CMD ["python", "/app/workload.py", "--mode", "baseline"]

FROM workload-base AS fuse
RUN apt-get update \
    && apt-get install -y --no-install-recommends fuse libfuse2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY fusefs.py /app/fusefs.py
CMD ["python", "/app/workload.py", "--mode", "fuse"]
