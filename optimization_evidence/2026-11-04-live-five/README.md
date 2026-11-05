# Opt71 — five live RTSPS streams


### Completed live five-camera result — FAILED throughput target

Opt71unchanged;five-minuteconfiguredsteadytest after25.3sstartup+10swarmup. Cumulativewindowbetweenfirst/lastsamples285.224s. Allfiveconnected;no capturedfatalerror, no decodererrors/reconnects in finalhealth. Nonetheless everycamera missedthe90%targetcriterion; totalaverageselectedFPS15.98/40. This isNOT a five-camera8FPS pass, even though healthstates remainedhealthy.

| Source | Average selected FPS (target8) | Whole-frame processing lifetime avg ms | Gait processing lifetime avg ms/call |
|---|---:|---:|---:|
| ch9 | 1.51 | 625.7 | 141.2 |
| ch10 | 1.92 | 496.2 | 122.8 |
| ch16 | 7.00 | 106.3 | 128.1 |
| traffic1 | 1.88 | 482.7 | 83.3 |
| traffic2 | 3.66 | 260.7 | 86.7 |

Sourceprobes:ch9/ch10/ch16HEVC2560x1440, advertised25FPS;traffic1H2641920x1080(avg18FPS,r_frame_rate36),traffic2HEVC1920x1080(r_frame_rate25,avgunknown). These differ materiallyfromearlier720pcrowdreplays. Metadataframerates are not measurednetworkarrivalrates. No resolutiondownscale/thresholdchange attempted.

Memoryfirst/finalsteadysample4939.04→7103.96MB,max7111.61MB. Growth isobserved;five minutesdoesnotestablishleakvswarming/highwatermark. FinalcontainerCPU4.555cores;GPU46.51%/NPU47.24%areNODEscopedincludingotherprograms, notattributedcomputeutilization. Finalcamera last-frameages1.076–4.256s; freshnesslabelalone hidesslowprocessing. SharedprocessRSS mustnotbesummedfive times.

Captured3928packages/1947evidence/625embeddings:79face139body75gait332scene;13plate_read and418plate_outcome. Theseestablishoutputexecution toMOCKagent, notrecognitionaccuracy oractualManagementsnapshotretrieval. Sampledhealth3920durable versusfinalcapture3928 reflectscontinuingcaptureuntilshutdown; not an8-packagelossclaim. Handoffbackpressure9,exhausted0,protocolerrors0.

Gaitcounterstotal1649inferencesavoided and75eligible. Yetgaitprocessing~83–141ms/call remains expensive: includesCPUfull-frameMOG/morphology/sequencecollection, notjustNPU. Existingmeasurementlabels attributeallthisstagewalltimetoNPU, so do NOTreadthat ashardwarebusy or actualgaitmodelcallcount. _features invokescollectonceperperson; full-frameMOG isreapplied percall. This supportsprioritizingpreprocessing/collectionprofile, but a once-per-frameMOG rewritechangesstate evolution and needsparity review. Othercontributors include~27–38msfullframeconversion andserialper-objectbody/plate work. Do not inferGPU/NPUdevicefault or blanketasyncbenefitfrom theseaveragesalone.

Nextboundedwork: splitgaitpreprocessing/model/queue timings accurately; investigateper-personfullframework andretainedmemory; thenoptimizewithoutchangingcollectionhistory/gates. Nativefaceadmissionandsecondaryoverlapremaincandidates. No runtime/image/contract/policy changes inthislive-testturn. No pushes. Testcontainerandagentcleanedup. Archiveoptimization_evidence/2026-11-04-live-five containsconfiguration,report,health,sourceprobes,compressedlogs/packages andwrapper. Sourceartifact/tmp/sentry-v8-capacity-5.m6ozh82b. No one-hoursoak and no claimofmaximumhardwarecapacity.
