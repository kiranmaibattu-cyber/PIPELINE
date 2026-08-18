"""PLATF use-case plugins. Each implements PLATF.core.Plugin.

Order in the host matters: the identity (re-id) plugin runs FIRST so it binds
(camera, local_id) -> Person before the analytics plugins see the observation.
Planned: reid, face, intrusion, loitering, counting, absence.
"""
