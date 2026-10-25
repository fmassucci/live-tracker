"""Find ONVIF cameras on the local network and ask them for their RTSP stream URLs."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from portaria.network import local_ipv4

_MULTICAST = ("239.255.255.250", 3702)
_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>uuid:{id}</w:MessageID>"
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
    "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
)


def discover(timeout: float = 3.0) -> dict[str, list[str]]:
    """Camera IP -> ONVIF device service URLs, from a WS-Discovery probe sent on every local network."""
    probe = _PROBE.format(id=uuid.uuid4()).encode()
    found: dict[str, list[str]] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        for address in local_ipv4() or ["0.0.0.0"]:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            sock.sendto(probe, _MULTICAST)
        sock.settimeout(0.3)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _port) = sock.recvfrom(65535)
            except (TimeoutError, ConnectionResetError):
                continue
            reply = data.decode(errors="ignore")
            if not is_camera(reply):
                continue
            urls = found.setdefault(ip, [])
            urls.extend(url for url in parse_xaddrs(reply) if url not in urls)
    return {ip: urls for ip, urls in found.items() if urls}


def is_camera(probe_match: str) -> bool:
    """Other WS-Discovery devices (Windows PCs, NAS boxes) answer the probe too; cameras say what they are."""
    return "NetworkVideoTransmitter" in probe_match or "onvif://www.onvif.org" in probe_match


def parse_xaddrs(probe_match: str) -> list[str]:
    match = re.search(r"<(?:\w+:)?XAddrs>([^<]+)<", probe_match)
    return match.group(1).split() if match else []


def stream_urls(device_url: str, user: str, password: str) -> list[tuple[str, str]]:
    """(profile name, RTSP URL) for each media profile.

    A dual-lens camera usually lists each lens, and its main and sub stream, as separate profiles.
    The URLs usually come without credentials.
    """
    capabilities = _call(device_url, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>", user, password)
    media = re.search(r"<(?:\w+:)?Media>\s*<(?:\w+:)?XAddr>([^<]+)<", capabilities)
    media_url = media.group(1).strip() if media else device_url
    profiles = _call(media_url, "<trt:GetProfiles/>", user, password)
    urls = []
    for token, name in re.findall(r'<(?:\w+:)?Profiles\b[^>]*\btoken="([^"]+)"[^>]*>.*?<(?:\w+:)?Name>([^<]*)<', profiles, re.S):
        request = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>"
        )
        uri = re.search(r"<(?:\w+:)?Uri>([^<]+)<", _call(media_url, request, user, password))
        if uri:
            urls.append((name, uri.group(1).strip()))
    return urls


def _call(url: str, body: str, user: str, password: str) -> str:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{_security_header(user, password)}<s:Body>{body}</s:Body></s:Envelope>"
    )
    request = urllib.request.Request(url, envelope.encode(), {"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode(errors="ignore")


def _security_header(user: str, password: str) -> str:
    """WS-Security UsernameToken with a password digest, which is what ONVIF cameras expect."""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1"'
        ' xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<UsernameToken><Username>{escape(user)}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</Password>"
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</Nonce>"
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created></UsernameToken></Security></s:Header>"
    )
