FROM alexxit/go2rtc:latest

# Ship a conservative baseline config inside the image. On first start the
# entrypoint copies it into /config (the mounted state directory) so go2rtc can
# persist WebUI/API stream edits and still work in a clean deployment.
COPY go2rtc.yaml /default/go2rtc.yaml

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/bin/sh", "-c", "if [ ! -f /config/go2rtc.yaml ]; then cp /default/go2rtc.yaml /config/go2rtc.yaml; fi; exec go2rtc -config /config/go2rtc.yaml"]
