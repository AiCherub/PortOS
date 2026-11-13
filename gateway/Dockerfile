# syntax=docker/dockerfile:1
#
# PortOS gateway image. It hands out sessions, watches them and
# serves the viewer.
#
# Alpine, like the session image: the server is pulled for every deployment
# and there is no reason for it to carry a distribution it does not use.
#
FROM alpine:3.23 AS python-runtime

RUN apk add --no-cache python3

FROM python-runtime AS dependencies

RUN apk add --no-cache py3-pip
WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --ignore-installed --prefix /install -r requirements.txt

FROM python-runtime

RUN apk add --no-cache tzdata

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
COPY --from=dependencies /install/ /usr/

# The static docker CLI, used by scripts/verify.py to inspect live sessions.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

COPY server.py .

EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3)"

CMD ["python", "server.py"]
