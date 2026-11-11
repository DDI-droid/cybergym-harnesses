FROM cybergym-harness-base:1
RUN pip install --no-cache-dir "dspy==3.4.1"
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV DENO_PATH=/usr/local/bin/deno
WORKDIR /workspace
