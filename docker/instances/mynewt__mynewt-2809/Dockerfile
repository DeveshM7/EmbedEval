FROM embedeval-mynewt-base:latest
ARG BASE_COMMIT
RUN git init /testbed \
    && cd /testbed \
    && git remote add origin https://github.com/apache/mynewt-core.git \
    && git fetch --depth 1 origin ${BASE_COMMIT} \
    && git checkout --detach FETCH_HEAD
COPY metadata.json test_patch.diff /opt/benchmark/
RUN cd /testbed \
    && git apply /opt/benchmark/test_patch.diff \
    && git add -A \
    && git -c user.name=EmbedEval -c user.email=benchmark@localhost commit -m "Benchmark starting snapshot with regression tests"
RUN mkdir -p /project/repos /project/targets/unittest \
    && ln -s /testbed /project/repos/apache-mynewt-core \
    && printf '%s\n' \
        'project.name: benchmark' \
        'project.repositories:' \
        '    - apache-mynewt-core' \
        'repository.apache-mynewt-core:' \
        '    type: github' \
        '    vers: 0.0.0' \
        '    user: apache' \
        '    repo: mynewt-core' \
        > /project/project.yml \
    && printf '%s\n' \
        'target.bsp: "@apache-mynewt-core/hw/bsp/native"' \
        'target.build_profile: "debug"' \
        'target.compiler: "@apache-mynewt-core/compiler/sim"' \
        > /project/targets/unittest/target.yml \
    && printf '%s\n' \
        'pkg.name: targets/unittest' \
        'pkg.type: target' \
        'pkg.cflags:' \
        '    - -Wno-error=stringop-overflow' \
        > /project/targets/unittest/pkg.yml \
    && python3 -c 'import json; from pathlib import Path; p = Path("/opt/benchmark/metadata.json"); m = json.loads(p.read_text()); p.write_text(json.dumps({k: m[k] for k in ("test_path", "baseline_tests", "fail_to_pass", "pass_to_pass")}))'
COPY run_tests.sh /usr/local/bin/run_tests
RUN chmod +x /usr/local/bin/run_tests
WORKDIR /testbed
