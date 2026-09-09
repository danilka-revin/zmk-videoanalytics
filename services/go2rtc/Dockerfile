FROM alexxit/go2rtc:latest

# Ship a conservative baseline config inside the image. On first start the
# entrypoint copies it into /config (the mounted state directory) so go2rtc can
# persist WebUI/API stream edits and still work in a clean deployment.
COPY go2rtc.yaml /default/go2rtc.yaml
# Encoder templates for the Vesktop-style preview quality ladder. Kept out of
# the persisted /config file on purpose — see the header of that file.
COPY quality-presets.yaml /default/quality-presets.yaml

ENTRYPOINT ["/sbin/tini", "--"]
# go2rtc merges every -config file key by key, later files winning, and writes
# API/WebUI stream edits back to the FIRST file given. /config must therefore
# stay first so persistence keeps working exactly as before. The quality preset
# overlay is loaded after it, which is what lets EXISTING installations (whose
# /config/go2rtc.yaml was copied by an older image) pick up the zmk_motion /
# zmk_detail encoder templates. An unknown template name would otherwise be
# handed to ffmpeg verbatim and kill the variant stream.
CMD ["/bin/sh", "-c", "if [ ! -f /config/go2rtc.yaml ]; then cp /default/go2rtc.yaml /config/go2rtc.yaml; fi; exec go2rtc -config /config/go2rtc.yaml -config /default/quality-presets.yaml"]
