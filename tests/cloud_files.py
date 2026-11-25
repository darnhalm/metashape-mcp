"""Writers for tiny LAS 1.2 (point format 2) and binary PLY files used in tests and acceptance."""

import struct
from pathlib import Path


def write_las(path, points, scale=0.001):
    """points: iterable of (x, y, z, r, g, b) with colors 0-255."""
    points = list(points)
    xs, ys, zs = ([p[i] for p in points] for i in range(3))
    header = bytearray(227)
    header[0:4] = b"LASF"
    header[24], header[25] = 1, 2
    header[26:58] = b"MCP test".ljust(32, b"\0")
    header[58:90] = b"MCP test".ljust(32, b"\0")
    struct.pack_into("<HHHIIBHI", header, 90, 1, 2026, 227, 227, 0, 2, 26, len(points))
    struct.pack_into("<5I", header, 111, len(points), 0, 0, 0, 0)
    struct.pack_into("<3d3d", header, 131, scale, scale, scale, 0.0, 0.0, 0.0)
    struct.pack_into("<6d", header, 179, max(xs), min(xs), max(ys), min(ys), max(zs), min(zs))
    body = bytearray()
    for x, y, z, r, g, b in points:
        body += struct.pack("<3iHBBbBH3H", round(x / scale), round(y / scale), round(z / scale),
                            0, 0b00001001, 1, 0, 0, 0, r * 257, g * 257, b * 257)
    Path(path).write_bytes(bytes(header) + bytes(body))


def write_ply(path, points):
    points = list(points)
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(points)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
    body = b"".join(struct.pack("<3f3B", *point) for point in points)
    Path(path).write_bytes(header.encode("ascii") + body)
