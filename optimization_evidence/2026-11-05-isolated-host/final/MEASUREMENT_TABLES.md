# Isolated Opt79 measurement tables

Generated from analysis.json and timestamp-matched window-rate-audit.json. These are measurements, not an automatic production-readiness declaration.

| Case | Cameras × FPS | Matched counter inference FPS range | Active rate criterion | Original harness | RAM p95 MiB | CPU p95 cores | CPU package max °C | Scene API gap p95 s |
|---|---:|---:|---|---|---:|---:|---:|---:|
| a2-isolated-all-1080-1x8 | 1 × 8 | — | thermal_guardrail | — | — | — | — | — |
| a2-isolated-all-1080-2x8 | 2 × 8 | 8.00–8.01 | PASS rate only | True | 3176.96 | 1.52 | 105.00 | 2.12 |
| a2-isolated-all-1080-4x8 | 4 × 8 | 7.99–8.01 | PASS rate only | True | 4131.75 | 3.58 | 105.00 | 2.28 |
| a2-isolated-all-1080-4x10 | 4 × 10 | — | thermal_guardrail | — | — | — | — | — |
| a2-isolated-all-1440-2x8 | 2 × 8 | — | thermal_guardrail | — | — | — | — | — |
| a2-isolated-all-1440-4x8 | 4 × 8 | — | thermal_guardrail | — | — | — | — | — |
| a2-isolated-all-1440-4x10 | 4 × 10 | — | thermal_guardrail | — | — | — | — | — |
| a3-pl2-65w-isolated-all-1080-1x8 | 1 × 8 | — | thermal_guardrail | — | — | — | — | — |
| a4-turbo-off-isolated-all-1080-1x8 | 1 × 8 | 8.00–8.00 | PASS rate only | True | 2706.05 | 1.17 | 80.00 | 2.10 |
| a4-turbo-off-isolated-all-1080-2x8 | 2 × 8 | 8.00–8.00 | PASS rate only | True | 3177.51 | 1.77 | 75.00 | 2.11 |
| a4-turbo-off-isolated-all-1080-4x8 | 4 × 8 | 7.98–8.06 | PASS rate only | False | 4052.32 | 4.24 | 84.00 | 2.45 |
| a4-turbo-off-isolated-all-1080-2x10 | 2 × 10 | 10.00–10.23 | PASS rate only | True | 3245.98 | 2.35 | 82.00 | 2.21 |
| a4-turbo-off-isolated-all-1080-4x10 | 4 × 10 | 9.36–9.87 | below active rate criterion | False | 4107.26 | 4.72 | 86.00 | 3.01 |
| a4-turbo-off-isolated-all-1440-2x8 | 2 × 8 | 8.00–8.29 | PASS rate only | True | 3933.64 | 2.33 | 86.00 | 2.21 |
| a4-turbo-off-isolated-all-1440-4x8 | 4 × 8 | 7.44–7.58 | below active rate criterion | False | 5448.46 | 4.97 | 91.00 | 2.78 |
| a4-turbo-off-isolated-all-1440-4x10 | 4 × 10 | 7.67–7.94 | below active rate criterion | False | 5359.06 | 4.72 | 90.00 | 3.44 |
| a4-turbo-off-scale-all-6-crowd-1080p | 6 × 8 | 6.34–6.65 | below active rate criterion | False | 4992.72 | 6.28 | 90.00 | 3.52 |
| a4-turbo-off-scale-all-4-crowd-720p | 4 × 8 | 7.96–7.97 | PASS rate only | True | 3223.73 | 2.82 | 85.00 | 2.20 |
| a4-turbo-off-scale-all-8-crowd-720p | 8 × 8 | 5.76–6.10 | below active rate criterion | False | 4292.53 | 5.05 | 86.00 | 3.93 |
| a4-turbo-off-scale-scene-4-crowd-1080p | 4 × 8 | — | INVALID TEST INPUT | — | — | — | 64.00 | — |
| a4-turbo-off-scale-identity-4-crowd-1080p | 4 × 8 | — | INVALID TEST INPUT | — | — | — | 55.00 | — |
| a4-turbo-off-scale-vehicle-4-ANPR | 4 × 8 | — | INVALID TEST INPUT | — | — | — | 54.00 | — |
| a4-turbo-off-scale-mixed0.5-4-crowd-1080p | 4 × 8 | 8.00–8.02 | PASS rate only | True | 3511.16 | 3.34 | 76.00 | 2.37 |
| a4-turbo-off-scale-mixed0.5-8-crowd-1080p | 8 × 8 | 3.09–6.63 | below active rate criterion | False | 4696.76 | 5.63 | 82.00 | 6.73 |
| a4-turbo-off-scale-all-1080-8x10 | 8 × 10 | 4.44–4.60 | below active rate criterion | False | 5950.16 | 6.54 | 86.00 | 6.12 |
| a4-turbo-off-functional-presence-ANPR | 1 × 8 | 7.99–7.99 | PASS rate only | False | 1187.77 | 0.64 | 77.00 | 2.20 |
| a4-turbo-off-functional-presence-crowd-1080p | 1 × 8 | 7.99–7.99 | PASS rate only | True | 1252.24 | 0.77 | 72.00 | 2.11 |
| a4-turbo-off-functional-hazard-fire_smoke | 1 × 8 | 8.00–8.00 | PASS rate only | True | 1458.60 | 0.44 | 75.00 | 2.10 |
| a4-turbo-off-functional-all-ANPR | 1 × 8 | 8.01–8.01 | PASS rate only | False | 2732.36 | 1.19 | 79.00 | 2.14 |
| a4-turbo-off-functional-all-parking | 1 × 8 | 8.01–8.01 | PASS rate only | True | 2381.35 | 0.65 | 81.00 | 2.06 |
| a4-turbo-off-functional-all-loitering | 1 × 8 | 8.00–8.00 | PASS rate only | True | 2490.83 | 0.69 | 71.00 | 2.12 |
| a4-turbo-off-mixed-roles-0.25 | 4 × 8 | 7.99–8.01 | PASS rate only | True | 3483.71 | 3.16 | 76.00 | 2.53 |
| a4-turbo-off-mixed-roles-0.75 | 4 × 8 | 7.99–8.01 | PASS rate only | True | 3505.82 | 3.77 | 81.00 | 2.24 |
| a4-turbo-off-agent-fault-503 | 2 × 8 | 7.99–8.00 | PASS rate only | False | 3214.37 | 1.79 | 89.00 | 2.21 |
| a4-turbo-off-agent-fault-429 | 2 × 8 | 7.99–8.00 | PASS rate only | False | 3204.55 | 1.84 | 81.00 | 2.21 |
| a4-turbo-off-agent-fault-400 | 2 × 8 | 8.00–8.01 | PASS rate only | False | 3174.54 | 1.78 | 81.00 | 2.18 |
| a4-turbo-off-agent-fault-401 | 2 × 8 | 8.01–8.01 | PASS rate only | False | 3240.25 | 1.93 | 87.00 | 2.16 |
| a4-turbo-off-agent-fault-slow | 2 × 8 | 7.99–8.01 | PASS rate only | True | 3177.70 | 1.79 | 86.00 | 2.12 |
| a4-turbo-off-agent-fault-drop_ack | 2 × 8 | 8.01–8.01 | PASS rate only | True | 3252.59 | 1.90 | 84.00 | 2.30 |
| a4-turbo-off-source-outage-5 | 2 × 8 | 7.46–8.01 | below active rate criterion | False | 3233.46 | 1.72 | 76.00 | 2.13 |
| a4-turbo-off-source-outage-15 | 2 × 8 | 6.48–8.00 | below active rate criterion | True | 3215.71 | 1.95 | 76.00 | 2.14 |
| a4-turbo-off-source-outage-30 | 2 × 8 | 5.79–8.01 | below active rate criterion | True | 3205.57 | 1.67 | 76.00 | 2.17 |
| a4-turbo-off-live-rtsp-traffic1 | 1 × 8 | 5.00–5.00 | below active rate criterion | False | 3369.38 | 1.14 | 82.00 | 4.41 |
| a4-turbo-off-live-rtsp-traffic2 | 1 × 8 | 8.08–8.08 | below active rate criterion | False | 3145.87 | 1.08 | 78.00 | 2.65 |
| a4-turbo-off-live-rtsp-ch9 | 1 × 8 | 8.15–8.15 | PASS rate only | True | 2923.79 | 1.29 | 84.00 | 2.17 |
| a4-turbo-off-live-rtsps-ch9 | 1 × 8 | 8.51–8.51 | PASS rate only | True | 3027.73 | 1.20 | 78.00 | 2.11 |
| a4-turbo-off-live-rtsps-ch10 | 1 × 8 | 8.44–8.44 | PASS rate only | True | 2909.03 | 1.18 | 76.00 | 2.10 |
| a4-turbo-off-live-rtsps-ch16 | 1 × 8 | 8.01–8.01 | PASS rate only | True | 2973.92 | 0.87 | 81.00 | 2.08 |
| a4-turbo-off-live-rtsps-traffic1 | 1 × 8 | 4.82–4.82 | below active rate criterion | False | 3354.15 | 1.21 | 82.00 | 3.94 |
| a4-turbo-off-live-rtsps-traffic2 | 1 × 8 | 7.51–7.51 | below active rate criterion | False | 3247.21 | 1.42 | 74.00 | 2.69 |
| a4-turbo-off-live-rtsps-camera01 | 1 × 8 | 7.92–7.92 | PASS rate only | True | 2627.01 | 0.81 | 80.00 | 2.09 |
| a4-turbo-off-live-tcp-192.168.1.92-9003 | 1 × 8 | 5.27–5.27 | below active rate criterion | True | 2470.16 | 0.41 | 82.00 | 3.58 |
| a4-turbo-off-live-tcp-192.168.1.92-9004 | 1 × 8 | 5.72–5.72 | below active rate criterion | True | 2697.66 | 0.49 | 78.00 | 3.12 |
| a4-turbo-off-live-tcp-192.168.1.92-9006 | 1 × 8 | 7.68–7.68 | below active rate criterion | True | 2631.55 | 0.67 | 73.00 | 2.61 |
| a4-turbo-off-live-tcp-192.168.1.92-9008 | 1 × 8 | 2.88–2.88 | below active rate criterion | True | 2649.36 | 0.47 | 72.00 | 5.12 |
| a4-turbo-off-live-tcp-192.168.1.92-9010 | 1 × 8 | 0.20–0.20 | below active rate criterion | True | 2442.37 | 0.25 | 76.00 | 5.10 |
| a4-turbo-off-pre-stress-mixed-2 | 2 × 8 | 8.00–8.01 | PASS rate only | True | 3184.24 | 2.06 | 75.00 | 2.21 |
| a4-turbo-off-stress-60min-all-2x8 | 2 × 8 | — | INVALID TEST INPUT | — | — | — | 74.00 | — |
| a4-turbo-off-valid-bindings-scale-scene-4 | 4 × 8 | 8.00–8.01 | PASS rate only | True | 1642.88 | 2.20 | 72.00 | 2.10 |
| a4-turbo-off-valid-bindings-scale-scene-8 | 8 × 8 | 7.91–8.00 | PASS rate only | True | 2157.52 | 4.56 | 70.00 | 2.33 |
| a4-turbo-off-valid-bindings-scale-scene-12 | 12 × 8 | 5.64–6.07 | below active rate criterion | False | 2739.55 | 5.87 | 77.00 | 3.89 |
| a4-turbo-off-valid-bindings-scale-identity-4 | 4 × 8 | 7.94–8.00 | PASS rate only | True | 3331.81 | 3.69 | 86.00 | 2.24 |
| a4-turbo-off-marked-420-preflight-2x8 | 2 × 8 | 7.93–8.01 | PASS rate only | True | 3237.62 | 1.89 | 75.00 | 2.10 |
| a4-turbo-off-stress-60min-all-2x8-yuv420 | 2 × 8 | 7.98–8.00 | PASS rate only | True | 3422.27 | 2.04 | 77.00 | 2.14 |
| a4-turbo-off-valid-bindings-scale-identity-8 | 8 × 8 | 5.26–5.40 | below active rate criterion | False | 4755.09 | 6.75 | 91.00 | 4.33 |
| a4-turbo-off-valid-bindings-scale-vehicle-4 | 4 × 8 | 7.38–7.57 | below active rate criterion | False | 2694.01 | 3.06 | 84.00 | 3.07 |
| a4-turbo-off-edge-device-absent-GPU | 1 × 8 | — | timing incomplete | — | — | — | 82.00 | — |
| a4-turbo-off-edge-device-absent-NPU | 1 × 8 | — | timing incomplete | — | — | — | 57.00 | — |
| a4-turbo-off-edge-primary-model-unreadable | 1 × 8 | — | timing incomplete | — | — | — | 75.00 | — |
| a4-turbo-off-edge-action-config-updates | 2 × 8 | 7.99–8.00 | PASS rate only | True | 3211.69 | 1.88 | 85.00 | 2.14 |
| a4-turbo-off-edge-empty1080 | 1 × 8 | 7.99–7.99 | PASS rate only | True | 2412.82 | 0.36 | 79.00 | 2.01 |
| a4-turbo-off-edge-empty4k | 1 × 1 | 0.20–0.20 | below active rate criterion | True | 1514.32 | 0.04 | 62.00 | 5.02 |
| a4-turbo-off-edge-quiet720 | 1 × 8 | 0.20–0.20 | below active rate criterion | True | 2390.49 | 0.17 | 71.00 | 5.01 |
| a4-turbo-off-edge-motion-transition | 1 × 8 | 2.85–2.85 | below active rate criterion | True | 2491.36 | 0.64 | 79.00 | 6.18 |
| a4-turbo-off-edge-fire-clear | 1 × 8 | 2.43–2.43 | below active rate criterion | True | 1485.07 | 0.36 | 73.00 | 5.03 |
| a4-turbo-off-edge-portrait | 1 × 8 | 7.36–7.36 | below active rate criterion | True | 2524.31 | 0.70 | 68.00 | 2.13 |
| a4-turbo-off-edge-nonaligned | 1 × 8 | 8.01–8.01 | PASS rate only | True | 2531.05 | 0.85 | 69.00 | 2.10 |
| a4-turbo-off-edge-fps15 | 1 × 8 | 8.00–8.00 | PASS rate only | True | 2527.04 | 0.89 | 72.00 | 2.12 |
| a4-turbo-off-edge-fps25 | 1 × 8 | 7.99–7.99 | PASS rate only | True | 2566.89 | 0.60 | 81.00 | 2.06 |
| a4-turbo-off-edge-load-ramp-empty-busy-empty | 8 × 10 | 8.25–8.72 | below active rate criterion | False | 5724.55 | 6.37 | 84.00 | 4.65 |
| a4-turbo-off-edge-tcp-hevc | 1 × 8 | — | timing incomplete | — | — | — | 84.00 | — |
| a4-turbo-off-edge-source-fps15-request30 | 1 × 30 | 15.00–15.00 | below active rate criterion | False | 2494.15 | 1.26 | 80.00 | 2.12 |
| a4-turbo-off-edge-default-fps-omitted | 1 × 8 | 8.01–8.01 | PASS rate only | True | 2724.12 | 0.93 | 78.00 | 2.14 |
| a4-turbo-off-edge-mixed-fractional-fps | 3 × 8 | 4.00–10.00 | below active rate criterion | False | 3701.81 | 2.63 | 76.00 | 2.21 |
| a4-turbo-off-edge-action-socket_absent | 2 × 8 | 8.01–8.01 | PASS rate only | False | 3272.98 | 1.91 | 82.00 | 2.20 |
| a4-turbo-off-edge-action-socket_permissions | 2 × 8 | 8.00–8.00 | PASS rate only | False | 3202.38 | 1.78 | 77.00 | 2.19 |
| a4-turbo-off-edge-action-pause_agent | 2 × 8 | 7.99–8.00 | PASS rate only | True | 3175.05 | 1.86 | 79.00 | 2.20 |
| a4-turbo-off-edge-action-restart_agent | 2 × 8 | 8.00–8.00 | PASS rate only | False | 3171.74 | 1.83 | 76.00 | 2.23 |
| a4-turbo-off-edge-action-pause_worker | 2 × 8 | — | timing incomplete | False | 3200.10 | 0.18 | 81.00 | 2.09 |
| a4-turbo-off-edge-action-kill_worker | 2 × 8 | — | timing incomplete | False | 2537.29 | 2.70 | 74.00 | 2.12 |
| a4-turbo-off-edge-action-restart_container | 2 × 8 | 7.89–8.17 | below active rate criterion | False | 2390.84 | 2.98 | 85.00 | 2.14 |
| a4-turbo-off-edge-action-cpu_quota | 2 × 8 | 3.43–3.73 | below active rate criterion | False | 3167.73 | 1.01 | 77.00 | 5.70 |
| a4-turbo-off-edge-action-memory_pressure | 2 × 8 | 7.99–8.00 | PASS rate only | True | 4661.40 | 1.84 | 77.00 | 2.12 |
| a4-turbo-off-edge-action-state_readonly | 2 × 8 | — | timing incomplete | False | 3192.87 | 2.09 | 81.00 | 2.11 |
| a4-turbo-off-edge-api-drain-reconciliation | 2 × 8 | 8.01–8.04 | PASS rate only | True | 3193.79 | 2.74 | 78.00 | 2.13 |
| a4-turbo-off-edge-native-worker-pause-45s | 2 × 8 | 8.00–8.01 | timing incomplete | True | 3271.87 | 2.71 | 80.00 | 2.00 |
| a4-turbo-off-edge-groups-21x1-scene | 21 × 1 | 0.59–0.62 | below active rate criterion | False | 3637.60 | 1.46 | 87.00 | 8.03 |
| a4-turbo-off-edge-grouping-4-8cams | 8 × 8 | 6.26–6.64 | below active rate criterion | False | 7852.25 | 9.63 | 91.00 | 3.20 |
| a4-turbo-off-edge-grouping-5-8cams | 8 × 8 | 5.70–7.70 | below active rate criterion | False | 8022.52 | 9.57 | 97.00 | 3.79 |
| a4-turbo-off-edge-source-bandwidth-cap | 2 × 8 | 8.00–8.00 | below active rate criterion | True | 3230.89 | 2.13 | 94.00 | 3.58 |
| a4-turbo-off-edge-corrupt-tcp-peer | 2 × 8 | — | timing incomplete | — | — | — | 86.00 | 2.07 |
| a4-turbo-off-edge-secret-url-rotation | 1 × 8 | 8.00–8.00 | PASS rate only | True | 2716.94 | 1.04 | 89.00 | 2.10 |
| a4-turbo-off-edge-camera-add-remove | 2 × 8 | 7.99–8.01 | PASS rate only | False | 2583.41 | 1.84 | 77.00 | 2.17 |
| a4-turbo-off-edge-peer-unavailable-startup | 2 × 8 | — | timing incomplete | — | — | — | 80.00 | 2.06 |
| a4-turbo-off-edge-all-unavailable-startup | 1 × 8 | — | timing incomplete | — | — | — | 75.00 | — |
| a4-turbo-off-edge-live-tcp-fanout5 | 5 × 8 | 5.73–5.81 | below active rate criterion | True | 3368.64 | 1.70 | 83.00 | 3.38 |
| a4-turbo-off-edge-http-input | 1 × 8 | — | timing incomplete | — | — | — | 67.00 | — |
| a4-turbo-off-edge-https-input | 1 × 8 | — | timing incomplete | — | — | — | 63.00 | — |
| a4-turbo-off-edge-local-file-secret | 1 × 8 | — | timing incomplete | — | — | — | 59.00 | — |
| a4-turbo-off-edge-latency-720p | 1 × 8 | 8.01–8.01 | PASS rate only | True | 1165.79 | 0.43 | 76.00 | 2.01 |
| a4-turbo-off-edge-latency-1080p30 | 4 × 8 | 7.94–8.11 | PASS rate only | True | 3981.32 | 4.02 | 79.00 | 2.30 |
| a4-turbo-off-edge-latency-1440p | 4 × 8 | 8.00–8.47 | PASS rate only | False | 5299.03 | 4.81 | 87.00 | 2.69 |
| a4-turbo-off-compare-shared-background-0-crowd-1080p | 4 × 8 | 7.47–7.73 | below active rate criterion | False | 4086.57 | 4.89 | 96.00 | 3.61 |
| a4-turbo-off-compare-shared-background-1-crowd-1080p | 4 × 8 | 7.96–8.00 | PASS rate only | True | 4074.87 | 3.90 | 88.00 | 2.50 |
| a4-turbo-off-compare-shared-background-0-crowd-1440p | 4 × 8 | 4.73–5.28 | below active rate criterion | False | 5445.86 | 4.74 | 104.00 | 5.15 |
| a4-turbo-off-compare-shared-background-1-crowd-1440p | 4 × 8 | 7.06–7.35 | below active rate criterion | False | 5467.58 | 4.86 | 92.00 | 3.08 |
| a4-turbo-off-frozen-reference-opt76-crowd-1080p | 2 × 8 | 8.03–8.05 | below active rate criterion | False | 3221.71 | 2.47 | 94.00 | 3.05 |
| a4-turbo-off-frozen-reference-opt79-crowd-1080p | 2 × 8 | 8.00–8.01 | PASS rate only | True | 3268.60 | 1.84 | 81.00 | 2.15 |
| a4-turbo-off-frozen-reference-opt76-crowd-1440p | 4 × 8 | 5.40–5.47 | below active rate criterion | False | 5330.26 | 4.78 | 104.00 | 4.70 |
| a4-turbo-off-frozen-reference-opt79-crowd-1440p | 4 × 8 | 7.13–7.85 | below active rate criterion | False | 5378.80 | 4.95 | 94.00 | 2.92 |
| a4-turbo-off-edge-action-pause_worker-persistent-replay | 2 × 8 | 8.00–8.05 | timing incomplete | True | 3214.84 | 2.48 | 84.00 | 2.20 |
| a4-turbo-off-vehicle-lower-boundary-marked-2 | 2 × 8 | 7.99–8.00 | PASS rate only | True | 1995.49 | 1.73 | 82.00 | 2.09 |
| a4-turbo-off-vehicle-lower-boundary-marked-3 | 3 × 8 | 8.00–8.02 | PASS rate only | False | 2344.91 | 2.39 | 81.00 | 2.42 |

## Scope and interpretation

a2 is stock-power controlled replay. a3 uses short-term65W but turbo enabled. a4 disables CPU turbo and retains short-term65W; sustained limit45W throughout. Transitional runs and attempt1 source-mapping errors are invalid for capacity. All application profiles keep scene search on every camera. Quiet admitted/safety-sweep behavior is separate from continuous active inference.

Matched rates use exact counter tuples and camera snapshot timestamps from the independent1Hz observer. Dividing cached counters by HTTP poll intervals can fabricate FPS dips. An incomplete timing match remains unqualified. Fault, quiet and negative-input cases are not continuous-active capacity claims even when their rate column is below target.

RAM/CPU table statistics come from steady harness snapshots. Temperature includes startup. Scene gaps here include startup/fault/shutdown and are diagnostic, not an automatic cadence SLA verdict. Independent marker latency and steady-source cadence must be reconciled separately.

A rate candidate still requires freshness, every-camera scene coverage, complete outputs, no fatal native errors, and headroom before any deployment recommendation. API receiver validates CV handoff only; downstream Management is outside scope.
