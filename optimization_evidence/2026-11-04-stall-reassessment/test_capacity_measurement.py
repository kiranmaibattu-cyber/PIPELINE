import importlib.util
from pathlib import Path
root = next(p for p in Path(__file__).resolve().parents if (p/'scripts/v8_capacity_probe.py').is_file())
spec = importlib.util.spec_from_file_location('capacity_probe', root/'scripts/v8_capacity_probe.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def sample(t, a, b):
    return {'sample_phase':'steady', 'sample_monotonic_seconds':t, 'target_decode_fps':20,
            'per_camera':{c:{'totals':{'decoded_frames':n}} for c,n in [('a',a),('b',b)]}}


def test_fast_final_windows_do_not_hide_low_total_throughput():
    rows=[sample(0,0,0),sample(100,500,500),sample(130,800,800)]
    assert not module.steady_throughput(rows,.9)['passed']


def test_one_fast_camera_cannot_mask_slow_peer():
    assert not module.steady_throughput([sample(0,0,0),sample(10,150,50)],.9)['passed']


def test_counter_reset_is_not_a_valid_measurement():
    assert not module.steady_throughput([sample(0,100,100),sample(10,10,10)],.9)['passed']


def test_sustained_both_cameras_pass():
    assert module.steady_throughput([sample(0,100,100),sample(10,200,200)],.9)['passed']
