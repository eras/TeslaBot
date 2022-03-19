FROM debian:bullseye-slim AS builder

# works also:
# FROM ubuntu:20.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true apt-get install -y libssl-dev libolm-dev libffi-dev tzdata gcc python3-minimal python3-pip python3-typing-extensions git

VOLUME /data
WORKDIR /build

COPY requirements.txt requirements-slack.txt requirements-matrix.txt /build/
RUN pip install -r requirements.txt -r requirements-slack.txt -r requirements-matrix.txt
COPY README.md setup.py setup.cfg versioneer.py /build/
RUN apt-get purge -y libssl-dev libolm-dev libffi-dev gcc && apt-get autoremove -y
COPY .git /build/.git/
RUN git reset --hard && git clean -d -x -f && pip install .[slack,matrix] && apt-get purge -y python3-pip

FROM debian:bullseye-slim

RUN apt-get update && \
  DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true apt-get install -y python3-minimal && \
  rm -rf /var/lib/dpkg /var/lib/apt /var/cache/apt /usr/share/doc /build
COPY --from=builder /usr/local/lib/python3.9/ /usr/local/lib/python3.9/
WORKDIR /data
RUN echo; python3 -m teslabot --version; echo

CMD ["python3", "-m", "teslabot", "--config", "/data/config.ini"]
