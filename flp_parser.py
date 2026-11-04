"""
flp_parser.py — lightweight FL Studio .flp reader for PLMS.

Reads the FLP event stream directly (no third-party parser needed) and returns:

  parse_ok         False when the file cannot be read; every property is then None
                   ("unavailable") — the parser never substitutes default values
  tempo, channel_count, pattern_count, sample_count
  plugins          sorted list of distinct plugin names (instruments and effects)
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
import re
import struct

PLAYLIST_EVENT   = 233
PLAYLIST_ITEM_SZ = 60
TICKS_PER_BAR    = 96 * 4   # 96 PPQ, 4 beats per bar

# Plugin events: 201 = internal plugin name (empty for sampler / audio channels),
# 203 = display name of the channel or effect slot, 199 = FL Studio version string.
PLUGIN_INTERNAL_EVT = 201
PLUGIN_DISPLAY_EVT  = 203
WRAPPER_NAMES       = {'fruity wrapper'}          # VST/AU host: the real name is the display name


def _unavailable(reason: str = 'unreadable') -> dict:
    """Result for a file that could not be read.

    Nothing is guessed: every property is None ("unavailable") and parse_ok is False.
    The file is still captured and restorable; only its properties are unknown.
    """
    return dict(parse_ok=False, parse_error=reason,
                tempo=None, channel_count=None, pattern_count=None, sample_count=None,
                playlist_items=None, tracks_used=None, arrangement_bars=None,
                plugins=None, arrangement=None)


def _text(raw: bytes) -> str:
    """FL Studio text events are UTF-16-LE, null terminated (older files: ASCII)."""
    if len(raw) >= 2 and len(raw) % 2 == 0 and b'\x00' in raw:
        s = raw.decode('utf-16-le', errors='replace')
    else:
        s = raw.decode('latin-1', errors='replace')
    return s.split('\x00', 1)[0].strip()


def _plugin_name(internal: str, display: str) -> str:
    """Stable, human-readable plugin name for set comparison."""
    if not internal:
        return ''                                  # sampler / audio clip: not a plugin
    if internal.lower() in WRAPPER_NAMES and display:
        return re.sub(r'\s+#\d+$', '', display).strip() or internal
    return internal


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
    """Parse FL Studio .flp bytes. Never raises.

    On success returns parse_ok=True with every property filled in (a property the
    file does not contain is None, never a guess). On any failure returns the
    "unavailable" result: parse_ok=False and every property None.
    """
    try:
        if len(data) < 22 or data[:4] != b'FLhd':
            return _unavailable('not an FL Studio project file')
        hdr_tracks = struct.unpack_from('<H', data, 10)[0]
        if data[14:18] != b'FLdt':
            return _unavailable('project data block missing')
        declared = struct.unpack_from('<I', data, 18)[0]
        if declared and 22 + declared > len(data):
            return _unavailable('file is incomplete (truncated)')

        pos, end = 22, len(data)
        tempo, chan_count, sample_count = None, 0, 0
        legacy_pats = set()
        pending_type = None
        playlist = None
        plugins = set()
        pend = None                    # (internal_name, event_index, display_name)

        def flush():
            nonlocal pend
            if pend:
                name = _plugin_name(pend[0], pend[2] or '')
                if name:
                    plugins.add(name)
            pend = None

        idx = 0
        while pos < end:
            idx += 1
            etype = data[pos]; pos += 1

            if etype < 64:                     # BYTE
                if pos + 1 > end:
                    return _unavailable('file is incomplete (truncated)')
                val = data[pos]; pos += 1
                if etype == 21:                # channel type flag
                    pending_type = val

            elif etype < 128:                  # WORD
                if pos + 2 > end:
                    return _unavailable('file is incomplete (truncated)')
                pos += 2
                if etype == 64:                # new channel
                    chan_count += 1
                    if pending_type == 0:      # sampler / audio clip
                        sample_count += 1
                    pending_type = None

            elif etype < 192:                  # DWORD
                if pos + 4 > end:
                    return _unavailable('file is incomplete (truncated)')
                val = struct.unpack_from('<I', data, pos)[0]; pos += 4
                if etype == 156:               # master tempo * 1000
                    tempo = val / 1000.0
                if etype == 132:               # legacy pattern ref (fallback only)
                    legacy_pats.add(val & 0xFFFF)

            else:                              # TEXT / DATA (varint length)
                length = shift = 0
                while True:
                    if pos >= end:
                        return _unavailable('file is incomplete (truncated)')
                    b = data[pos]; pos += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                if pos + length > end:
                    return _unavailable('file is incomplete (truncated)')
                blob = data[pos:pos + length]
                if etype == PLAYLIST_EVENT:
                    playlist = blob
                elif etype == PLUGIN_INTERNAL_EVT:
                    flush()
                    pend = (_text(blob), idx, None)
                elif etype == PLUGIN_DISPLAY_EVT and pend and pend[2] is None and idx - pend[1] <= 3:
                    pend = (pend[0], pend[1], _text(blob))
                pos += length
        flush()

        if chan_count == 0 and hdr_tracks > 0:
            chan_count = hdr_tracks

        if playlist:
            arr = parse_playlist(playlist)
        else:
            arr = dict(playlist_items=0, tracks_used=0, arrangement_bars=0.0,
                       arrangement=dict(patterns={}, tracks={}, audio_clips=0))

        used = len(arr['arrangement']['patterns'])
        pattern_count = used if used else (len(legacy_pats) if legacy_pats else None)

        return dict(parse_ok=True, parse_error='',
                    tempo=round(tempo, 1) if tempo is not None else None,
                    channel_count=chan_count, pattern_count=pattern_count,
                    sample_count=sample_count, plugins=sorted(plugins, key=str.lower), **arr)
    except Exception as e:                     # malformed file: report, never guess
        logging.warning(f'[FLP Parser] {e}')
        return _unavailable('file could not be read')


def parse_flp(path: str) -> dict:
    try:
        with open(path, 'rb') as f:
            return parse_flp_bytes(f.read())
    except Exception as e:
        logging.warning(f'[FLP read] {path}: {e}')
        return _unavailable('file could not be read')
