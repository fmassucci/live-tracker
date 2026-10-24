from portaria import onvif
from portaria.camera import redact

CAPABILITIES = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><tds:GetCapabilitiesResponse><tds:Capabilities>
<tt:Media><tt:XAddr>http://192.168.1.50:8899/onvif/media_service</tt:XAddr>
<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast></tt:StreamingCapabilities></tt:Media>
</tds:Capabilities></tds:GetCapabilitiesResponse></s:Body></s:Envelope>"""

PROFILES = """<s:Envelope><s:Body><trt:GetProfilesResponse>
<trt:Profiles fixed="true" token="000"><tt:Name>mainStream</tt:Name>
<tt:VideoSourceConfiguration token="vs0"><tt:Name>VideoSource_0</tt:Name></tt:VideoSourceConfiguration></trt:Profiles>
<trt:Profiles token="001" fixed="true"><tt:Name>subStream</tt:Name></trt:Profiles>
</trt:GetProfilesResponse></s:Body></s:Envelope>"""


def _stream_uri(token: str) -> str:
    return f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://192.168.1.50:554/onvif{int(token) + 1}</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>"


def test_stream_urls_follows_capabilities_profiles_and_stream_uris(monkeypatch):
    calls = []

    def fake_call(url, body, user, password):
        calls.append(url)
        if "GetCapabilities" in body:
            return CAPABILITIES
        if "GetProfiles" in body:
            return PROFILES
        token = body.split("<trt:ProfileToken>")[1].split("<")[0]
        return _stream_uri(token)

    monkeypatch.setattr(onvif, "_call", fake_call)
    urls = onvif.stream_urls("http://192.168.1.50:8899/onvif/device_service", "admin", "pw")
    assert urls == [("mainStream", "rtsp://192.168.1.50:554/onvif1"), ("subStream", "rtsp://192.168.1.50:554/onvif2")]
    assert calls[1:] == ["http://192.168.1.50:8899/onvif/media_service"] * 3


def test_parse_xaddrs_reads_all_addresses():
    match = "<d:ProbeMatch><d:XAddrs>http://192.168.1.50:8899/onvif/device_service http://[fe80::1]/onvif</d:XAddrs></d:ProbeMatch>"
    assert onvif.parse_xaddrs(match) == ["http://192.168.1.50:8899/onvif/device_service", "http://[fe80::1]/onvif"]


def test_only_cameras_count_as_found():
    camera = "<d:ProbeMatch><d:Types>dn:NetworkVideoTransmitter</d:Types><d:XAddrs>http://192.168.0.50/onvif/device_service</d:XAddrs></d:ProbeMatch>"
    nas = "<wsd:ProbeMatch><wsd:Types>wsdp:Device pub:Computer</wsd:Types><wsd:XAddrs>http://nas:5357/x</wsd:XAddrs></wsd:ProbeMatch>"
    assert onvif.is_camera(camera)
    assert not onvif.is_camera(nas)


def test_redact_hides_the_password():
    assert redact("rtsp://admin:s3cret@192.168.1.50:554/onvif1") == "rtsp://***@192.168.1.50:554/onvif1"
    assert redact("0") == "0"
