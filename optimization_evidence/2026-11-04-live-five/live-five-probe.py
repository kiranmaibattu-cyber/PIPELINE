import sys
sys.path.insert(0, '/home/admin1/Documents/sentry_cv/scripts')
import v8_capacity_probe as probe
probe.SOURCE_URLS = [
 'rtsps://gpu-box.burmilla-bass.ts.net:443/' + name
 for name in ('ch9','ch10','ch16','traffic1','traffic2')
]
raise SystemExit(probe.main())
