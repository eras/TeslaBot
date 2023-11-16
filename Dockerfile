FROM debian:bullseye-slim AS builder

# works also:
# FROM ubuntu:20.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true apt-get install -y libssl-dev libolm-dev libffi-dev tzdata gcc python3-minimal python3-pip python3-typing-extensions git

VOLUME /data
WORKDIR /build

COPY requirements.txt requirements-slack.txt requirements-matrix.txt /build/
RUN pip install -r requirements.txt -r requirements-slack.txt -r requirements-matrix.txt
COPY .git /build/.git/
RUN git reset --hard && pip install .[slack,matrix]

FROM debian:bullseye-slim

RUN apt-get update && \
  DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true apt-get install -y libolm3 libffi7 python3-minimal && \
  rm -rf /var/lib/dpkg /var/lib/apt /var/cache/apt /usr/share/doc /build
COPY --from=builder /usr/local/lib/ /usr/local/lib/
WORKDIR /data
RUN echo; python3 -m teslabot --version && echo

CMD ["python3", "-m", "teslabot", "--config", "/data/config.ini"]
