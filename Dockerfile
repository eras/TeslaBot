FROM python:3.7-bullseye

RUN apt-get update && apt-get install -y libssl-dev libolm-dev libffi-dev tzdata gcc

VOLUME /data
WORKDIR /app

RUN cp /usr/share/zoneinfo/Europe/Helsinki /etc/localtime
RUN echo "Europe/Helsinki" > /etc/timezone

COPY requirements.txt requirements-slack.txt requirements-matrix.txt /app/
RUN pip install -r requirements.txt -r requirements-slack.txt -r requirements-matrix.txt
COPY README.md setup.py /app/
RUN apt-get purge -y libssl-dev libolm-dev libffi-dev gcc && apt-get autoremove -y && rm -rf /var/lib/dpkg /var/lib/apt /var/cache/apt
COPY teslabot /app/teslabot/
RUN pip install .[slack,matrix]

CMD ["python", "-m", "teslabot", "/data/teslabot.ini"]
