# Image for running untrusted test suites. Mend starts each run with no network,
# a read-only root filesystem, dropped capabilities and a non-root user.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.3.3 \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandbox
USER 10001:10001
WORKDIR /work
