"""
flp_parser.py — lightweight FL Studio .flp reader for PLMS.

Reads the FLP event stream directly (no third-party parser needed) and returns:

  tempo, channel_count, pattern_count, sample_count      (as before)
  playlist_items   number of clips placed on the playlist
  tracks_used      number of distinct playlist tracks that hold clips
  arrangement_bars length of the arrangement in bars (4/4, 96 PPQ)
  arrangement      {'patterns': {pattern_no: instances},
                    'tracks':   {track_no:   clips},
                    'audio_clips': n}  — used by the compare view

Playlist event (id 233) in FL Studio 21+/24 stores 60-byte items:
  u32 position(ticks) | u16 pattern_base | u16 item_index | u32 length(ticks)
  | u16 track_rvidx | u16 group | ...
item_index > pattern_base  → pattern clip (pattern no = item_index - pattern_base)
item_index <= pattern_base → audio clip / automation clip (channel index)
track no = 500 - track_rvidx (verified against sample project files).
"""
import logging
import struct

PLAYLIST_EVENT   = 233
PLAYLIST_ITEM_SZ = 60
TICKS_PER_BAR    = 96 * 4   # 96 PPQ, 4 beats per bar
EMPTY = dict(tempo=120.0, channel_count=0, pattern_count=0, sample_count=0,
             playlist_items=0, tracks_used=0, arrangement_bars=0.0,
             arrangement=dict(patterns={}, tracks={}, audio_clips=0))


def _empty(**over):
    d = {k: (dict(patterns={}, tracks={}, audio_clips=0) if k == 'arrangement' else v)
         for k, v in EMPTY.items()}
    d.update(over)
    return d


def parse_playlist(blob: bytes) -> dict:
    """Decode the playlist event payload into arrangement statistics."""
    patterns, tracks = {}, {}
    audio = 0
    max_end = 0
    n = len(blob) // PLAYLIST_ITEM_SZ
    for i in range(n):
        pos, base, idx, length, trk, _grp = struct.unpack_from('<IHHIHH', blob, i * PLAYLIST_ITEM_SZ)
        track_no = 500 - trk
        if track_no < 1 or track_no > 500:
            continue                      # corrupt / unknown item layout
        tracks[track_no] = tracks.get(track_no, 0) + 1
        if idx > base:
            pat = idx - base
            patterns[pat] = patterns.get(pat, 0) + 1
        else:
            audio += 1
        max_end = max(max_end, pos + length)
    return dict(
        playlist_items   = sum(tracks.values()),
        tracks_used      = len(tracks),
        arrangement_bars = round(max_end / TICKS_PER_BAR, 1),
        arrangement      = dict(patterns={str(k): v for k, v in sorted(patterns.items())},
                                tracks={str(k): v for k, v in sorted(tracks.items())},
                                audio_clips=audio),
    )


def parse_flp_bytes(data: bytes) -> dict:
    """Parse FL Studio .flp bytes. Never raises — returns safe defaults on error."""
    try:
        if data[:4] != b'FLhd':
            return _empty()

        hdr_tracks = struct.unpack_from('<H', data, 10)[0]
        if data[14:18] != b'FLdt':
            return _empty(channel_count=hdr_tracks)

        pos, end = 22, len(data)
        tempo, chan_count, sample_count = 120.0, 0, 0
        legacy_pats = set()
        pending_type = None
        playlist = None

        while pos < end - 1:
            etype = data[pos]; pos += 1

            if etype < 64:                     # BYTE
                val = data[pos]; pos += 1
                if etype == 21:                # channel type flag
                    pending_type = val

            elif etype < 128:                  # WORD
                pos += 2
                if etype == 64:                # new channel
                    chan_count += 1
                    if pending_type == 0:      # sampler / audio clip
                        sample_count += 1
                    pending_type = None

            elif etype < 192:                  # DWORD
                val = struct.unpack_from('<I', data, pos)[0]; pos += 4
                if etype == 156:               # master tempo * 1000
                    tempo = val / 1000.0
                if etype == 132:               # legacy pattern ref (fallback only)
                    legacy_pats.add(val & 0xFFFF)

            else:                              # TEXT / DATA (varint length)
                length = shift = 0
                while pos < end:
                    b = data[pos]; pos += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                if etype == PLAYLIST_EVENT:
                    playlist = data[pos:pos + length]
                pos = min(pos + length, end)

        if chan_count == 0 and hdr_tracks > 0:
            chan_count = hdr_tracks

        arr = parse_playlist(playlist) if playlist else \
            dict(playlist_items=0, tracks_used=0, arrangement_bars=0.0,
                 arrangement=dict(patterns={}, tracks={}, audio_clips=0))

        used = len(arr['arrangement']['patterns'])
        pattern_count = used if used else (len(legacy_pats) if legacy_pats else 1)

        return dict(tempo=round(tempo, 1), channel_count=chan_count,
                    pattern_count=pattern_count, sample_count=sample_count, **arr)
    except Exception as e:                     # malformed file — fail soft
        logging.warning(f'[FLP Parser] {e}')
        return _empty()


def parse_flp(path: str) -> dict:
    try:
        with open(path, 'rb') as f:
            return parse_flp_bytes(f.read())
    except Exception as e:
        logging.warning(f'[FLP read] {path}: {e}')
        return _empty()
