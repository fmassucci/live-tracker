import re
import socket
import threading

from portaria.camera import probe

SDP = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Camera\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
       "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n")


def _camera_without_tcp() -> tuple[int, list[str]]:
    """A fake RTSP camera that, like some cheap Wi-Fi ones, answers a TCP request with a UDP transport.

    It has no video to send: asked for UDP, it refuses too. Returns its port and the transports asked for.
    """
    asked: list[str] = []
    server = socket.create_server(("127.0.0.1", 0))

    def answer(request: str) -> str:
        cseq = re.search(r"CSeq: *(\d+)", request).group(1)
        if request.startswith("DESCRIBE"):
            return (f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
                    f"Content-Length: {len(SDP)}\r\n\r\n{SDP}")
        if request.startswith("SETUP"):
            tcp = "/TCP" in re.search(r"Transport: *(\S+)", request).group(1)
            asked.append("TCP" if tcp else "UDP")
            if tcp:
                return (f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 1\r\n"
                        "Transport: RTP/AVP;unicast;client_port=5000-5001;server_port=6000-6001\r\n\r\n")
            return f"RTSP/1.0 461 Unsupported Transport\r\nCSeq: {cseq}\r\n\r\n"
        return f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n\r\n"

    def talk(connection: socket.socket) -> None:
        with connection:
            data = b""
            try:
                while chunk := connection.recv(4096):
                    data += chunk
                    while b"\r\n\r\n" in data:
                        head, data = data.split(b"\r\n\r\n", 1)
                        connection.sendall(answer(head.decode()).encode())
            except OSError:
                pass

    def serve() -> None:
        while True:
            threading.Thread(target=talk, args=(server.accept()[0],), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], asked


def test_a_camera_that_wont_stream_over_tcp_is_asked_for_udp():
    port, asked = _camera_without_tcp()
    info, _ = probe(f"rtsp://127.0.0.1:{port}/onvif1", seconds=0.5)
    assert not info["opened"]  # the fake camera has no video
    # One transport per attempt shows the option reaches FFmpeg, which on Windows reads it through msvcrt.dll
    assert asked == ["TCP", "UDP"]
