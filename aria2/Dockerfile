FROM alpine:3.20

RUN apk add --no-cache aria2 curl tzdata

COPY aria2.conf /etc/aria2/aria2.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY on-complete.sh /usr/local/bin/on-complete.sh

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/on-complete.sh

EXPOSE 6800

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]