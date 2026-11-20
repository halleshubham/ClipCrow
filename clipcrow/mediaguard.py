"""Untrusted media in, safely: ffmpeg/ffprobe pick a demuxer by CONTENT, and some demuxers (playlists, concat scripts) read other files or fetch URLs. So a file a person or a
worker gave us is first checked to start like a real audio/video container, and is opened with only the ``file`` and ``pipe`` protocols and only container formats we expect."""
from __future__ import annotations

from pathlib import Path

# ffmpeg's own names for the demuxers we accept (a playlist/concat/image2 demuxer is not on the list)
FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,mp3,wav,flac,ogg,aac,flv,asf,mpeg"
LOCAL = ["-protocol_whitelist", "file,pipe", "-format_whitelist", FORMATS]                    # before -i, for a file on disk
REMOTE = ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-format_whitelist", FORMATS]   # before -i, for a presigned link the server made


class NotMedia(ValueError):
    pass


def looks_like_media(head: bytes) -> bool:
    """The first bytes of a known container: MP4/MOV/3GP (ftyp...), Matroska/WebM (EBML), AVI/WAV (RIFF), MP3 (ID3 or a frame), ADTS AAC, FLAC, Ogg, FLV, ASF, MPEG-PS, MPEG-TS."""
    h = head[:4096]
    if len(h) < 12:
        return False
    if h[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot"):
        return True
    if h[:4] == b"\x1a\x45\xdf\xa3" or (h[:4] == b"RIFF" and h[8:12] in (b"AVI ", b"WAVE")):
        return True
    if h[:3] == b"ID3" or h[:4] in (b"fLaC", b"OggS", b"FLV\x01") or h[:4] == b"\x30\x26\xb2\x75" or h[:4] == b"\x00\x00\x01\xba":
        return True
    if h[0] == 0xFF and h[1] & 0xE0 == 0xE0:                                                 # MP3 / ADTS frame sync
        return True
    return h[0] == 0x47 and len(h) > 376 and h[188] == 0x47 and h[376] == 0x47                # MPEG-TS packets


def check_head(head: bytes) -> None:
    if not looks_like_media(head):
        raise NotMedia("That file is not a video or audio file.")


def check_file(path: str | Path) -> None:
    with open(path, "rb") as f:
        check_head(f.read(4096))
