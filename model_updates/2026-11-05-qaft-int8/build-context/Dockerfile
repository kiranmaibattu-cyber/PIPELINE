FROM localhost/sentry-v8-opt79-shared-gait-background:latest
USER 0:0
COPY surface_stream.py /opt/traffic-pilot/services/worker/pipeline/surface_stream.py
COPY stream_fleet_openvino.py /opt/traffic-pilot/services/worker/stream_fleet_openvino.py
COPY worker.json /opt/traffic-pilot/config/worker.json
COPY model.xml model.bin model_static_b1.xml model_static_b1.bin export_manifest.json integration.json /models/traffic/openvino/vehicle_person_qaft_int8/
RUN rm -f /models/traffic/openvino/vehicle_yolo26n_fp16_dynamic.xml /models/traffic/openvino/vehicle_yolo26n_fp16_dynamic.bin /models/traffic/openvino/vehicle_yolo26n_fp16_static_b1.xml /models/traffic/openvino/vehicle_yolo26n_fp16_static_b1.bin \
    && python3 -m py_compile /opt/traffic-pilot/services/worker/pipeline/surface_stream.py /opt/traffic-pilot/services/worker/stream_fleet_openvino.py
ENV SENTINEL_GVA_VEHICLE_MODEL=/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml
LABEL org.opencontainers.image.version="2026.11.05-v8-qaft-int8-model-candidate" \
      sentinel.optimization.experiment="qaft-int8-model-swap-unqualified" \
      io.apexfabric.detector.profile="vehicle-person-qaft-int8" \
      io.apexfabric.detector.active-batch="1" \
      io.apexfabric.detector.checkpoint.sha256="8fa716bf26f92037522256a50a38fa6fb1de830277bf6f440c02146f8beae000"
USER 10001:10001
