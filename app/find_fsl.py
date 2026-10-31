"""Find Full Spectrum laser controllers on the local network.

Sends the same 4-byte locator broadcast RetinaEngrave sends (UDP port 23) and
lists every board that answers as an "FSL Laser". Read-only: the board only
replies with its identity. No root needed; replies come back to our port.
"""
import socket
import sys
import time

PROBE = bytes([0xFF, 0x04, 0x02, 0xFB])


def find(wait=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.sendto(PROBE, ("255.255.255.255", 23))
    s.settimeout(0.2)
    found, end = [], time.monotonic() + wait
    while time.monotonic() < end:
        try:
            data, (ip, _) = s.recvfrom(2048)
        except socket.timeout:
            continue
        text = data.decode("ascii", "replace")
        found.append((ip, text.strip("\0 "), data.hex(" ")))
    return found


if __name__ == "__main__":
    hits = find()
    for ip, text, raw in hits:
        tag = "FULL SPECTRUM" if "FSL Laser" in text else "other device"
        print(f"{ip}  [{tag}]  {''.join(c if c.isprintable() else '.' for c in text)}")
        print(f"    raw: {raw}")
    if not hits:
        print("nothing answered")
    sys.exit(0 if hits else 1)
