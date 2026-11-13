"""Run in server image with /app/scripts mounted; no Docker daemon needed."""
import sys
from types import SimpleNamespace
import docker

captured = {}


class Captured(BaseException):
    pass


def create(**kwargs):
    captured.update(kwargs)
    raise Captured


docker.from_env = lambda: SimpleNamespace(containers=SimpleNamespace(create=create, list=lambda **_: []))
sys.path.insert(0, '/app')
import server

for enabled in ('true', 'false'):
    server.SESSION_AUDIO = enabled
    launch = {'APP_CMD': 'wine', 'APP_ARGS': '["notepad.exe"]'}
    try:
        server.create_session(launch)
    except Captured:
        pass
    assert captured['environment']['AUDIO_ENABLED'] == enabled
    container = SimpleNamespace(attrs={'Config': {'Env': [
        f'{key}={value}' for key, value in captured['environment'].items()
    ]}})
    assert server.matches_launch(container, launch)
    server.SESSION_AUDIO = 'false' if enabled == 'true' else 'true'
    assert not server.matches_launch(container, launch)
print('PASS: master setting reaches sessions; wrong-mode warm containers are rejected')
