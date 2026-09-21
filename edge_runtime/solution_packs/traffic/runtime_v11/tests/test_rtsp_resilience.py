import pytest

from services.worker.decode import _ffmpeg_cmd, _network_input_options, _parse_probe_resolution


def test_rtsp_decoder_uses_tcp_and_supported_ffmpeg_timeout():
    command = _ffmpeg_cmd("rtsp://camera.test/live", 5, True)

    assert command[command.index("-rtsp_transport") + 1] == "tcp"
    assert command[command.index("-timeout") + 1] == "30000000"
    assert "-rw_timeout" not in command


def test_http_decoder_uses_timeout_and_live_reconnect_without_rtsp_options():
    command = _ffmpeg_cmd("https://camera.test/live.m3u8", 8, True)

    assert command[command.index("-rw_timeout") + 1] == "30000000"
    assert command[command.index("-reconnect") + 1] == "1"
    assert command[command.index("-reconnect_streamed") + 1] == "1"
    assert command[command.index("-reconnect_on_network_error") + 1] == "1"
    assert command[command.index("-reconnect_delay_max") + 1] == "10"
    assert "-rtsp_transport" not in command
    assert "-stream_loop" not in command


def test_http_probe_uses_reconnect_options():
    options = _network_input_options("http://camera.test/mjpeg", probe=True)

    assert options[options.index("-rw_timeout") + 1] == "30000000"
    assert options[options.index("-reconnect") + 1] == "1"
    assert "-rtsp_transport" not in options


def test_file_decoder_still_loops_and_is_real_time():
    command = _ffmpeg_cmd("/video/test.mp4", 5, False)

    assert command[command.index("-stream_loop") + 1] == "-1"
    assert "-re" in command
    assert "-reconnect" not in command


def test_probe_parser_accepts_hls_response_with_repeated_stream_entries():
    payload = '{"programs":[{"streams":[{"width":3840,"height":2160}]}],"streams":[' \
              '{"width":3840,"height":2160},{"width":3840,"height":2160}]}'

    assert _parse_probe_resolution(payload) == (3840, 2160)


def test_probe_parser_rejects_missing_dimensions():
    with pytest.raises(ValueError, match="no video dimensions"):
        _parse_probe_resolution('{"streams":[{}]}')
