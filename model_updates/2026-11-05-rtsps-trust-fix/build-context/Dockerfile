FROM localhost/sentry-v8-qaft-int8-model-candidate:2026.11.05
USER 0:0
COPY source_tls.py surface_stream.py multi_camera_gst.py /opt/traffic-pilot/services/worker/pipeline/
ENV SENTINEL_CAMERA_CA_DIR=/run/secrets/sentinel/camera-ca
RUN python3 -m py_compile /opt/traffic-pilot/services/worker/pipeline/source_tls.py /opt/traffic-pilot/services/worker/pipeline/surface_stream.py /opt/traffic-pilot/services/worker/pipeline/multi_camera_gst.py \
    && python3 -c "from gi.repository import Gio; assert Gio.TlsBackend.get_default().supports_tls()"
LABEL org.opencontainers.image.version="2026.11.05-v8-qaft-int8-tlsfix1" \
      io.apexfabric.camera-tls.policy="verify-with-deployment-ca" \
      io.apexfabric.camera-tls.default-ca-directory="/run/secrets/sentinel/camera-ca" \
      sentinel.optimization.experiment="qaft-int8-candidate-tls-trust-repair"
USER 10001:10001
