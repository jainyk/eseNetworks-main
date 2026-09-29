FROM python:3.12-slim

WORKDIR /app
COPY server.py /app/server.py

# Keep large data in a separate layer and make each trial's layer digest unique.
ARG PAYLOAD_MIB=256
ARG PAYLOAD_SEED=1
RUN python -c "import os, random; size=int(os.environ.get('PAYLOAD_MIB', '256')); seed=int(os.environ.get('PAYLOAD_SEED', '1')); rng=random.Random(seed); f=open('/app/payload.bin','wb'); [f.write(rng.randbytes(min(1024*1024, size*1024*1024-i))) for i in range(0, size*1024*1024, 1024*1024)]; f.close()" \
    && chmod 0444 /app/payload.bin

EXPOSE 8080
CMD ["python", "/app/server.py"]
