#!/usr/bin/env python3
"""Run the capacity harness with two independent bindings sharing scene scope.

Qualification only: exercises compute reuse without changing any event policy.
"""
from copy import deepcopy
import v8_capacity_probe as probe

_original = probe.desired_state


def shared_scene_state(*args, **kwargs):
    state = _original(*args, **kwargs)
    for camera in state['cameras']:
        scene = next((b for b in camera['bindings'] if b['app'] == 'scene_search'), None)
        if scene is None:
            raise ValueError('shared scene qualification requires --full-usecases')
        duplicate = deepcopy(scene)
        duplicate['use_case_id'] += '-second-binding'
        camera['bindings'].append(duplicate)
    return state


if __name__ == '__main__':
    probe.desired_state = shared_scene_state
    raise SystemExit(probe.main())
