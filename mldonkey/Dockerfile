FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends mldonkey-server ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
COPY on_complete.sh /usr/local/bin/on_complete.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /usr/local/bin/on_complete.sh
EXPOSE 4000 4080 4662 4665 4672
VOLUME ["/var/lib/mldonkey", "/downloads"]
ENTRYPOINT ["/entrypoint.sh"]