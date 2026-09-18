from services.worker.decode import _ffmpeg_cmd


def test_rtsp_decoder_uses_tcp_and_supported_ffmpeg_timeout():
    command = _ffmpeg_cmd("rtsp://camera.test/live", 5, True)

    assert command[command.index("-rtsp_transport") + 1] == "tcp"
    assert command[command.index("-timeout") + 1] == "30000000"
    assert "-rw_timeout" not in command
