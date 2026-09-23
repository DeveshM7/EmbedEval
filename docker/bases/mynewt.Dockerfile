FROM ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git golang-go gcc-multilib g++-multilib make file python3 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/apache/mynewt-newt.git /opt/newt-src && cd /opt/newt-src && git checkout c36db0f3df0a2787dfa19914d79a687d6a6cb53a && go build -o /usr/local/bin/newt ./newt
COPY docker/shared/mynewt_runner.py /usr/local/bin/mynewt_run_tests
COPY docker/shared/setup_mynewt.py /opt/benchmark/setup.py
RUN chmod +x /usr/local/bin/mynewt_run_tests
WORKDIR /testbed
