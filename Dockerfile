FROM python:3.7-bullseye

RUN apt-get update && apt-get install -y libssl-dev libolm-dev libffi-dev tzdata gcc

VOLUME /data
WORKDIR /build

RUN cp /usr/share/zoneinfo/Europe/Helsinki /etc/localtime
RUN echo "Europe/Helsinki" > /etc/timezone

COPY requirements.txt requirements-slack.txt requirements-matrix.txt /build/
RUN pip install -r requirements.txt -r requirements-slack.txt -r requirements-matrix.txt
COPY README.md setup.py setup.cfg versioneer.py /build/
RUN apt-get purge -y libssl-dev libolm-dev libffi-dev gcc && apt-get autoremove -y && rm -rf /var/lib/dpkg /var/lib/apt /var/cache/apt
COPY .git /build/.git/
RUN git reset --hard; git clean -d -x -f
RUN pip install .[slack,matrix]
WORKDIR /data
RUN rm -rf /build
RUN echo; python -m teslabot --version; echo

CMD ["python", "-m", "teslabot", "--config", "/data/teslabot.ini"]
