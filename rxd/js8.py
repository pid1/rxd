#!/usr/bin/env python3
"""js8: rxd's JS8 receiver.

Receive only. It takes slots of 12 kHz audio, finds JS8 signals in them and
turns them into JS8 frames and messages:

  candidates    a spectrogram one symbol long, a quarter symbol apart, scored
                against the three 7x7 Costas arrays every JS8 signal carries
  refinement    each candidate is cut out of one FFT of the whole slot, mixed
                to baseband at 24 samples a symbol, then timed to 1/24 symbol
                and tuned to 1/16 of a tone against the Costas arrays
  soft symbols  58 data symbols x 8 tones -> 174 bit log-likelihoods
  LDPC(174,87)  belief propagation, all candidates at once, then CRC-12
  subtraction   decoded signals are rebuilt and taken out of the slot, and
                the slot is searched again for what they were hiding
  frames        72 bits -> heartbeat, CQ, compound callsign, directed command
                or data frame -> text, then frames are joined into messages

Run as a script it serves decodes for rxd over a pipe (--serve), decodes WAV
files (--wav), builds the JSC word table (--jsc-import-source), or reports
what it is (--version). See README.md for the
protocol sources and their licences: no JS8Call or WSJT-X code is used here.
"""
import json
import os
import sys
import time

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np  # noqa: E402
from scipy import fft as sfft  # noqa: E402
from scipy import ndimage  # noqa: E402
from scipy import special  # noqa: E402

VERSION = "rxd-js8-1"
RATE = 12000

# ------------------------------------------------------------ the waveform

# 79 symbols: Costas A, 29 parity symbols, Costas B, 29 message symbols,
# Costas C. Each symbol is one of 8 tones, a tone apart; the tone is the
# 3-bit word itself (no Gray code). Normal mode uses one Costas array three
# times; the faster and slower modes use three different ones, which is how
# a Normal decoder ignores them.
COSTAS_ORIGINAL = ((4, 2, 5, 6, 1, 3, 0),) * 3
COSTAS_MODIFIED = ((0, 6, 2, 3, 5, 4, 1), (1, 5, 0, 2, 3, 6, 4), (2, 5, 0, 6, 4, 1, 3))
NSYM = 79
SYNC_AT = (0, 36, 72)
DATA_AT = tuple(range(7, 36)) + tuple(range(43, 72))


class Submode:
    """One JS8 speed: samples per symbol at 12 kHz, the slot, and when in the
    slot a transmission starts."""

    def __init__(self, name, letter, sps, slot, delay, costas, snr_floor):
        self.name, self.letter, self.sps, self.slot = name, letter, sps, slot
        self.delay, self.costas, self.snr_floor = delay, costas, snr_floor
        self.baud = RATE / sps
        self.duration = NSYM * sps / RATE
        # Audio kept either side of the slot, so a signal up to 2.5 s early or
        # late (a station's clock, or the audio path) is still whole.
        self.pre = min(2.0, slot * 0.15)
        self.post = 0.5 if slot >= 10 else 0.25

    def __repr__(self):
        return "Submode(%s)" % self.name


SUBMODES = {
    "normal": Submode("normal", "A", 1920, 15, 0.5, COSTAS_ORIGINAL, -24),
    "fast": Submode("fast", "B", 1200, 10, 0.2, COSTAS_MODIFIED, -22),
    "turbo": Submode("turbo", "C", 600, 6, 0.1, COSTAS_MODIFIED, -20),
    "slow": Submode("slow", "E", 3840, 30, 0.5, COSTAS_MODIFIED, -28),
}
BY_LETTER = {m.letter: m for m in SUBMODES.values()}

# ------------------------------------------------------------- the code

# The (174,87) LDPC code's generator, one row per parity bit, 87 message bits
# each, MSB first (hex), and the order its 174 bits go on the air. Both are
# FT8 v1's, as published in Karlis Goba's MIT-licensed ft8_lib (2018): the
# 87 parity bits and then the 87 message bits, the bit at i sent at
# COLORDER[i].
GEN = (
    "23bba830e23b6b6f50982e", "1f8e55da218c5df3309052", "ca7b3217cd92bd59a5ae20", "56f78313537d0f4382964e",
    "29c29dba9c545e267762fe", "6be396b5e2e819e373340c", "293548a138858328af4210", "cb6c6afcdc28bb3f7c6e86",
    "3f2a86f5c5bd225c961150", "849dd2d63673481860f62c", "56cdaec6e7ae14b43feeee", "04ef5cfa3766ba778f45a4",
    "c525ae4bd4f627320a3974", "fe37802941d66dde02b99c", "41fd9520b2e4abeb2f989c", "40907b01280f03c0323946",
    "7fb36c24085a34d8c1dbc4", "40fc3e44bb7d2bb2756e44", "d38ab0a1d2e52a8ec3bc76", "3d0f929ef3949bd84d4734",
    "45d3814f504064f80549ae", "f14dbf263825d0bd04b05e", "f08a91fb2e1f78290619a8", "7a8dec79a51e8ac5388022",
    "ca4186dd44c3121565cf5c", "db714f8f64e8ac7af1a76e", "8d0274de71e7c1a8055eb0", "51f81573dd4049b082de14",
    "d037db825175d851f3af00", "d8f937f31822e57c562370", "1bf1490607c54032660ede", "1616d78018d0b4745ca0f2",
    "a9fa8e50bcb032c85e3304", "83f640f1a48a8ebc0443ea", "eca9afa0f6b01d92305edc", "3776af54ccfbae916afde6",
    "6abb212d9739dfc02580f2", "05209a0abb530b9e7e34b0", "612f63acc025b6ab476f7c", "0af7723161ec223080be86",
    "a8fc906976c35669e79ce0", "45b7ab6242b77474d9f11a", "b274db8abd3c6f396ea356", "9059dfa2bb20ef7ef73ad4",
    "3d188ea477f6fa41317a4e", "8d9071b7e7a6a2eed6965e", "a377253773ea678367c3f6", "ecbd7c73b9cd34c3720c8a",
    "b6537f417e61d1a7085336", "6c280d2a0523d9c4bc5946", "d36d662a69ae24b74dcbd8", "d747bfc5fd65ef70fbd9bc",
    "a9fa2eefa6f8796a355772", "cc9da55fe046d0cb3a770c", "f6ad4824b87c80ebfce466", "cc6de59755420925f90ed2",
    "164cc861bdd803c547f2ac", "c0fc3ec4fb7d2bb2756644", "0dbd816fba1543f721dc72", "a0c0033a52ab6299802fd2",
    "bf4f56e073271f6ab4bf80", "57da6d13cb96a7689b2790", "81cfc6f18c35b1e1f17114", "481a2a0df8a23583f82d6c",
    "1ac4672b549cd6dba79bcc", "c87af9a5d5206abca532a8", "97d4169cb33e7435718d90", "a6573f3dc8b16c9d19f746",
    "2c4142bf42b01e71076acc", "081c29a10d468ccdbcecb6", "5b0f7742bca86b8012609a", "012dee2198eba82b19a1da",
    "f1627701a2d692fd9449e6", "35ad3fb0faeb5f1b0c30dc", "b1ca4ea2e3d173bad4379c", "37d8e0af9258b9e8c5f9b2",
    "cd921fdf59e882683763f6", "6114e08483043fd3f38a8a", "2e547dd7a05f6597aac516", "95e45ecd0135aca9d6e6ae",
    "b33ec97be83ce413f9acc8", "c8b5dffc335095dcdcaf2a", "3dd01a59d86310743ec752", "14cd0f642fc0c5fe3a65ca",
    "3a0a1dfd7eee29c2e827e0", "8abdb889efbe39a510a118", "3f231f212055371cf3e2a2",
)
COLORDER = (
    0, 1, 2, 3, 30, 4, 5, 6, 7, 8, 9, 10, 11, 32, 12, 40, 13, 14, 15, 16, 17, 18, 37, 45, 29, 19,
    20, 21, 41, 22, 42, 31, 33, 34, 44, 35, 47, 51, 50, 43, 36, 52, 63, 46, 25, 55, 27, 24, 23, 53,
    39, 49, 59, 38, 48, 61, 60, 57, 28, 62, 56, 58, 65, 66, 26, 70, 64, 69, 68, 67, 74, 71, 54, 76,
    72, 75, 78, 77, 80, 79, 73, 83, 84, 81, 82, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97,
    98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116,
    117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135,
    136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154,
    155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173,
)

# The sparse parity checks: each row lists the codeword bits (in GEN's order,
# parity then message) that sum to zero. They are not copied from anywhere: they were derived
# from GEN alone, as the 87 lowest-weight words of the code's dual (weights
# 5 to 7, each bit in exactly 3 checks), found by information-set search.
# test_js8 checks every row against GEN.
NM = (
    (12, 45, 81, 87, 138), (5, 39, 77, 108, 140), (5, 41, 78, 104, 139), (46, 54, 61, 93, 136),
    (12, 50, 56, 100, 127, 144), (25, 50, 69, 116, 140, 159), (41, 58, 63, 98, 141, 172),
    (30, 56, 58, 115, 131, 161), (4, 46, 85, 99, 116, 170), (10, 23, 68, 102, 135, 164),
    (14, 45, 55, 110, 145, 170), (43, 58, 79, 91, 145, 171), (2, 36, 59, 106, 123, 166),
    (2, 49, 67, 100, 139, 169), (16, 53, 80, 98, 129, 157), (9, 51, 61, 92, 127, 163),
    (44, 65, 72, 92, 141, 166), (11, 53, 65, 99, 125, 154), (19, 55, 72, 115, 124, 153),
    (7, 33, 63, 95, 119, 151), (7, 72, 84, 100, 130, 167), (47, 50, 82, 93, 123, 158),
    (11, 36, 78, 88, 145, 168), (3, 13, 59, 91, 120, 148), (1, 59, 60, 102, 137, 156),
    (10, 22, 67, 98, 126, 153), (9, 49, 81, 89, 140, 168), (1, 4, 56, 89, 118, 146),
    (1, 32, 42, 92, 121, 149), (27, 49, 82, 109, 135, 160), (37, 46, 67, 103, 131, 143),
    (11, 37, 62, 87, 118, 147), (48, 51, 77, 105, 142, 148), (18, 30, 65, 102, 117, 159),
    (14, 28, 74, 89, 128, 155), (20, 54, 80, 96, 114, 166), (20, 34, 52, 95, 131, 160),
    (43, 64, 85, 97, 133, 152), (32, 64, 80, 105, 130, 164), (3, 38, 75, 114, 126, 167),
    (26, 41, 81, 112, 137, 167), (25, 34, 75, 111, 139, 156), (2, 31, 55, 90, 119, 147),
    (20, 28, 79, 108, 138, 153), (15, 26, 76, 106, 136, 164), (19, 39, 75, 97, 129, 155),
    (0, 31, 73, 104, 135, 163), (0, 24, 52, 88, 117, 146), (40, 44, 79, 104, 143, 150),
    (29, 54, 78, 109, 134, 165), (6, 31, 86, 103, 144, 168), (29, 33, 70, 112, 144, 152),
    (38, 47, 76, 88, 121, 157), (4, 19, 83, 112, 120, 160), (8, 37, 84, 110, 117, 165),
    (21, 23, 74, 84, 132, 161), (6, 32, 62, 94, 123, 150), (27, 28, 62, 107, 137, 151),
    (26, 35, 42, 91, 129, 158), (6, 38, 63, 110, 133, 154), (17, 30, 70, 101, 130, 158),
    (17, 57, 87, 111, 120, 163), (10, 13, 52, 94, 127, 155), (18, 51, 83, 90, 128, 157),
    (18, 34, 86, 113, 124, 171), (0, 35, 83, 107, 126, 173), (8, 35, 69, 96, 124, 152),
    (7, 24, 71, 109, 142, 150), (9, 40, 68, 97, 125, 151), (25, 36, 74, 105, 122, 162),
    (17, 24, 85, 114, 122, 149), (33, 48, 73, 99, 121, 161), (23, 29, 42, 90, 143, 172),
    (48, 60, 69, 94, 136, 141), (12, 22, 73, 101, 133, 162), (8, 40, 66, 101, 128, 169),
    (14, 15, 71, 96, 125, 156), (5, 13, 66, 93, 122, 147), (16, 57, 60, 108, 119, 165),
    (47, 57, 68, 115, 142, 173), (22, 44, 76, 111, 134, 170), (21, 53, 82, 113, 138, 149),
    (21, 43, 77, 103, 134, 146), (15, 16, 86, 107, 116, 148, 169),
    (27, 39, 70, 106, 132, 154, 171), (45, 64, 66, 95, 132, 159, 173),
    (3, 61, 71, 113, 118, 162, 172),
)

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-+"
ALPHABET_INDEX = {c: i for i, c in enumerate(ALPHABET)}
CRC_POLY = 0xC06   # CRC-12, x^12 + x^11 + x^10 + x^2 + x (the x^12 implied)
CRC_XOR = 42       # JS8's CRC differs from FT8 v1's by this constant

_GEN = np.array([[(int(r, 16) >> (87 - j)) & 1 for j in range(87)] for r in GEN], np.uint8)
_NMAX = max(len(r) for r in NM)
# Check -> its bits, padded with 174, a dummy bit that is always 0.
_CV = np.array([list(r) + [174] * (_NMAX - len(r)) for r in NM], np.intp)
_REAL = _CV < 174
# Bit -> the flat positions of its three edges in _CV.
_VE = np.zeros((174, 3), np.intp)
_fill = np.zeros(174, np.intp)
for _e, _v in enumerate(_CV.ravel()):
    if _v < 174:
        _VE[_v, _fill[_v]] = _e
        _fill[_v] += 1
assert (_fill == 3).all()
del _e, _v, _fill


def crc12(bits75):
    """JS8's CRC of the 72 payload bits and 3 frame bits: an augmented CRC-12
    over them and 13 zero bits (the CRC's place, rounded up to 11 bytes)."""
    reg = 0
    for b in list(bits75) + [0] * 13:
        top = reg >> 11
        reg = ((reg << 1) & 0xFFF) | int(b)
        if top:
            reg ^= CRC_POLY
    return reg ^ CRC_XOR


def frame_bits(frame, i3):
    """12 characters and the 3 transmission bits -> the 87 message bits."""
    if len(frame) != 12 or any(c not in ALPHABET_INDEX for c in frame):
        raise ValueError("a frame is 12 characters of %s" % ALPHABET)
    v = 0
    for c in frame:
        v = (v << 6) | ALPHABET_INDEX[c]
    bits = [(v >> (71 - i)) & 1 for i in range(72)] + [(i3 >> (2 - i)) & 1 for i in range(3)]
    crc = crc12(bits)
    return bits + [(crc >> (11 - i)) & 1 for i in range(12)]


def bits_frame(bits87):
    """The 87 decoded message bits -> (frame, i3), or None if the CRC fails."""
    b = [int(x) for x in bits87]
    crc = 0
    for x in b[75:87]:
        crc = (crc << 1) | x
    if crc12(b[:75]) != crc:
        return None
    v = 0
    for x in b[:72]:
        v = (v << 1) | x
    frame = "".join(ALPHABET[(v >> (66 - 6 * i)) & 63] for i in range(12))
    return frame, (b[72] << 2) | (b[73] << 1) | b[74]


_COLORDER = np.array(COLORDER, np.intp)


def encode(frame, i3):
    """The 174-bit codeword: 87 parity bits, then the 87 message bits."""
    m = np.array(frame_bits(frame, i3), np.uint8)
    return np.concatenate([(_GEN.astype(np.int32) @ m) % 2, m]).astype(np.uint8)


def to_air(cw):
    """A codeword (or its bits' likelihoods) in the order sent, and back."""
    out = np.empty_like(cw)
    out[..., _COLORDER] = cw
    return out


def from_air(bits):
    return bits[..., _COLORDER]


def tones(frame, i3, submode="normal"):
    """The 79 tones (0-7) a transmitter sends for one frame."""
    m = SUBMODES[submode] if isinstance(submode, str) else submode
    cw = to_air(encode(frame, i3))
    words = cw[0::3] * 4 + cw[1::3] * 2 + cw[2::3]
    out = np.zeros(NSYM, np.int8)
    for a, at in enumerate(SYNC_AT):
        out[at:at + 7] = m.costas[a]
    out[list(DATA_AT)] = words
    return out


def synth(tone_seq, f0, submode="normal", amp=1.0, phase=0.0):
    """Phase-continuous 8-FSK audio at 12 kHz: tone 0 at f0 Hz (for tests)."""
    m = SUBMODES[submode] if isinstance(submode, str) else submode
    f = f0 + np.repeat(np.asarray(tone_seq, float), m.sps) * m.baud
    ph = phase + 2 * np.pi * np.cumsum(f) / RATE
    return (amp * np.sin(ph)).astype(np.float32)


def check_parity(cw):
    c = np.append(np.asarray(cw, np.uint8), 0)
    return not (c[_CV].sum(1) % 2).any()


def bp_decode(llr, iters=30, patience=8):
    """Belief propagation on a batch: llr (B, 174), positive meaning 1.
    A row is dropped once its count of failed checks hasn't improved for
    patience iterations (it is noise, or too weak). Returns (bits (B, 174)
    uint8, ok (B,) bool, iterations used (B,))."""
    llr = np.atleast_2d(np.asarray(llr, np.float64))
    nb = llr.shape[0]
    out = (llr > 0).astype(np.uint8)
    done = np.zeros(nb, bool)
    used = np.full(nb, iters, np.int32)
    rows = np.arange(nb)
    L = np.concatenate([-llr, np.full((nb, 1), 50.0)], 1)  # log P(0)/P(1)
    r = np.zeros((nb, _CV.size))
    best = np.full(nb, 999)
    since = np.zeros(nb, np.int32)
    flat = _CV.ravel()
    pad = ~_REAL.ravel()
    for it in range(iters + 1):
        tot = L.copy()
        tot[:, :174] += r[:, _VE].sum(2)
        hard = np.concatenate([(tot[:, :174] < 0), np.zeros((len(rows), 1), bool)], 1)
        nbad = (hard[:, _CV].sum(2) % 2).sum(1)
        good = nbad == 0
        if good.any():
            out[rows[good]] = hard[good, :174]
            used[rows[good]] = it
            done[rows[good]] = True
        better = nbad < best
        since = np.where(better, 0, since + 1)
        best = np.minimum(best, nbad)
        keep = ~good & (since < patience)
        if it == iters or not keep.any():
            break
        if not keep.all():
            rows, L, r, tot, best, since = rows[keep], L[keep], r[keep], tot[keep], best[keep], since[keep]
        q = tot[:, flat] - r
        t = np.tanh(np.clip(q, -40, 40) / 2).reshape(len(rows), _CV.shape[0], _NMAX)
        t = np.where(np.abs(t) < 1e-12, 1e-12, t)
        sgn = np.sign(t)
        la = np.log(np.abs(t))
        ex = sgn.prod(2, keepdims=True) * sgn * np.exp(la.sum(2, keepdims=True) - la)
        r = (2 * np.arctanh(np.clip(ex, -0.999999, 0.999999))).reshape(len(rows), -1)
        r[:, pad] = 0
    return out, done, used

_GFULL = np.concatenate([_GEN.T, np.eye(87, dtype=np.uint8)], 1)   # message bit -> codeword
_PAIRS = np.array([(i, j) for i in range(87) for j in range(i + 1, 87)], np.intp)


def osd_decode(llr, order=2):
    """Ordered-statistics decoding (Fossorier and Lin): take the 87 most
    reliable independent bits as given, re-encode, and try flipping one or two
    of them. Returns (codeword, discrepancy): the sum of |llr| over the bits
    where the codeword disagrees with the received hard decisions."""
    llr = np.asarray(llr, np.float64)
    hard = (llr > 0).astype(np.uint8)
    rel = np.abs(llr)
    perm = np.argsort(-rel, kind="stable")
    G = _GFULL[:, perm].copy()
    piv = []
    r = 0
    for col in range(174):
        nz = np.nonzero(G[r:, col])[0]
        if len(nz) == 0:
            continue
        p = r + nz[0]
        if p != r:
            G[[r, p]] = G[[p, r]]
        mask = G[:, col].astype(bool)
        mask[r] = False
        G[mask] ^= G[r]
        piv.append(col)
        r += 1
        if r == 87:
            break
    hp, rp = hard[perm], rel[perm]
    info = hp[piv]
    c0 = (info.astype(np.int32) @ G) % 2
    c0 = c0.astype(np.uint8)
    cands = [c0[None]]
    if order >= 1:
        cands.append(c0[None] ^ G)
    if order >= 2:
        cands.append(c0[None] ^ G[_PAIRS[:, 0]] ^ G[_PAIRS[:, 1]])
    C = np.concatenate(cands)
    dis = (C != hp[None]).astype(np.float32) @ rp.astype(np.float32)
    best = int(np.argmin(dis))
    cw = np.empty(174, np.uint8)
    cw[perm] = C[best]
    return cw, float(dis[best])


# ------------------------------------------------------------ the decoder

class Decoder:
    """Decodes one submode's slots. decode(x, t0) takes 12 kHz audio whose
    first sample is t0 seconds from the slot's start (negative: before it)."""

    SPS_DS = 24          # baseband samples per symbol
    LLR_MAX = 6.0
    # Ordered-statistics decoding is slow (tens of ms a candidate on a CM4),
    # so it gets only the strongest few candidates belief propagation failed
    # on, and a result only counts if it disagrees with what was received
    # on little of the total confidence (true decodes ~0.04, noise >0.06).
    OSD_SYNC, OSD_MAX, OSD_DISCREPANCY = 4.0, 6, 0.045
    DT_MAX = {"normal": 2.5, "fast": 2.0, "turbo": 1.0, "slow": 3.0}

    def __init__(self, submode="normal", fmin=200.0, fmax=3000.0, max_candidates=None,
                 sync_min=1.25, passes=2, iters=30):
        self.m = SUBMODES[submode] if isinstance(submode, str) else submode
        self.fmin, self.fmax = fmin, fmax
        self.max_candidates = max_candidates or 120
        self.sync_min, self.passes, self.iters = sync_min, passes, iters
        self.dt_max = self.DT_MAX[self.m.name]
        self.D = self.m.sps // self.SPS_DS
        c = self.m.costas
        self.sync_pos = np.array([at + i for at in SYNC_AT for i in range(7)])
        self.sync_tone = np.array([c[a][i] for a in range(3) for i in range(7)])
        self.ones = [[t for t in range(8) if (t >> (2 - b)) & 1] for b in range(3)]
        self.zeros = [[t for t in range(8) if not (t >> (2 - b)) & 1] for b in range(3)]
        # Each Costas symbol's tone, shifted by -4/16 .. +4/16 of a tone.
        self._shifts = self.m.baud * np.arange(-4, 5) / 16
        n = np.arange(self.SPS_DS)
        frac = self.sync_tone[:, None] + np.arange(-4, 5)[None, :] / 16
        self._ref = np.exp(-2j * np.pi * frac[:, :, None] * n / self.SPS_DS).astype(np.complex64)

    def window(self):
        """(before, after): seconds of audio decode() wants either side of the
        slot, so every start time it searches is whole."""
        m = self.m
        before = max(0.0, self.dt_max - m.delay) + 0.1
        after = max(0.0, m.delay + self.dt_max + m.duration - m.slot) + 0.1
        return before, after

    # ---------------------------------------------------------- stage 1
    def candidates(self, x, t0):
        m = self.m
        sps, hop = m.sps, m.sps // 4
        nfr = (len(x) - sps) // hop + 1
        if nfr < 4 * NSYM:
            return []
        frames = np.lib.stride_tricks.as_strided(
            x, shape=(nfr, sps), strides=(hop * x.strides[0], x.strides[0]), writeable=False)
        bw = m.baud / 2
        k0 = max(1, int(self.fmin / bw))
        k1 = int(self.fmax / bw) + 1
        spec = sfft.rfft(frames, n=2 * sps, axis=1, workers=1)[:, k0:k1 + 16]
        P = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        nf = P.shape[1] - 16
        # Start frames whose dt (start - nominal delay) is within +-dt_max.
        lo = int(np.ceil((m.delay - self.dt_max - t0) * RATE / hop))
        hi = int(np.floor((m.delay + self.dt_max - t0) * RATE / hop))
        lo = max(lo, 0)
        hi = min(hi, nfr - 1 - 4 * 78)
        if hi < lo:
            return []
        nt = hi - lo + 1
        ta = np.zeros((3, nt, nf), np.float32)
        tt = np.zeros((3, nt, nf), np.float32)
        for a in range(3):
            for i in range(7):
                row = lo + 4 * (SYNC_AT[a] + i)
                blk = P[row:row + nt]
                ta[a] += blk[:, 2 * self.sync_tone[7 * a + i]:][:, :nf]
                for k in range(8):
                    tt[a] += blk[:, 2 * k:2 * k + nf]
        eps = 1e-12

        def ratio(sel):
            s, t = ta[sel].sum(0), tt[sel].sum(0)
            return s / ((t - s) / 7 + eps)

        score = np.maximum(ratio([0, 1, 2]), np.maximum(ratio([0, 1]), ratio([1, 2])))
        peak = ndimage.maximum_filter(score, size=(9, 3), mode="nearest")
        ti, fi = np.nonzero((score == peak) & (score >= self.sync_min))
        order = np.argsort(-score[ti, fi])[: self.max_candidates]
        out = []
        for j in order:
            out.append({"start": (lo + ti[j]) * hop, "freq": (k0 + fi[j]) * bw,
                        "sync": float(score[ti[j], fi[j]])})
        return out

    # ---------------------------------------------------------- stage 2
    def baseband(self, X, N, f):
        """The slot at 24 samples a symbol, tone 0 of a signal at f near 0 Hz."""
        M = N // self.D
        df = RATE / N
        kc = int(round(f / df))
        k = np.arange(-(M // 2), M - M // 2)
        idx = kc + k
        ok = (idx >= 0) & (idx < len(X))
        Y = np.zeros(M, np.complex64)
        Y[k[ok] % M] = X[idx[ok]]
        # Flat over the eight tones, rolled off beyond them.
        b = self.m.baud
        fk = k * df
        w = np.ones(M, np.float32)
        lo, hi = -1.5 * b, 8.5 * b
        w[fk < lo] = np.cos(np.clip((lo - fk[fk < lo]) / (2 * b), 0, 1) * np.pi / 2) ** 2
        w[fk > hi] = np.cos(np.clip((fk[fk > hi] - hi) / (2 * b), 0, 1) * np.pi / 2) ** 2
        Y *= w[k % M]
        return sfft.ifft(Y, workers=1).astype(np.complex64), kc * df

    def refine(self, y, start):
        """Best (start sample at 24/symbol, frequency shift in Hz) near start:
        the Costas symbols' power, a symbol at a time, against each of the
        sync tones shifted by up to a quarter tone either way."""
        S = self.SPS_DS
        n0 = int(round(start / self.D))
        offs = np.arange(n0 - 8, n0 + 9)
        pad = 8 * S
        yp = np.concatenate([np.zeros(pad, y.dtype), y, np.zeros(NSYM * S + pad, y.dtype)])
        idx = (offs[:, None, None] + S * self.sync_pos[None, :, None] + np.arange(S)) + pad
        z = yp[idx]                                     # (offsets, 21, S)
        pw = np.abs(np.einsum("oin,idn->oid", z, self._ref)) ** 2
        s = pw.sum(1)                                   # (offsets, shifts)
        o, k = np.unravel_index(int(np.argmax(s)), s.shape)
        return int(offs[o]), float(self._shifts[k])

    def symbols(self, y, n, df):
        S = self.SPS_DS
        rate = self.m.baud * S
        pad = 2 * NSYM * S
        yp = np.concatenate([np.zeros(pad, y.dtype), y, np.zeros(pad, y.dtype)])
        idx = n + pad + S * np.arange(NSYM)[:, None] + np.arange(S)
        rot = np.exp(-2j * np.pi * df * (idx - pad) / rate).astype(np.complex64)
        return sfft.fft(yp[idx] * rot, axis=1, workers=1)[:, :8]

    def soft(self, Sy):
        """Bit log-likelihoods for non-coherent 8-FSK: each tone's Rician
        likelihood from the estimated signal and noise, summed over the tones
        whose word has the bit set, against those where it is clear."""
        P = np.abs(Sy[list(DATA_AT)]) ** 2
        n0 = float(np.median(np.sort(P, 1)[:, :7])) / np.log(2)
        if n0 <= 0:
            return np.zeros(174)
        es = max(float(P.max(1).mean()) - n0, 0.05 * n0)
        z = 2 * np.sqrt(es * P) / n0
        lt = np.log(special.i0e(z)) + z
        out = np.empty((58, 3))
        for b in range(3):
            out[:, b] = np.logaddexp.reduce(lt[:, self.ones[b]], 1) - \
                np.logaddexp.reduce(lt[:, self.zeros[b]], 1)
        return out.ravel()

    # ---------------------------------------------------------- the slot
    def decode(self, x, t0=0.0):
        x = np.ascontiguousarray(x, np.float32)
        m = self.m
        found, seen = [], set()
        for _ in range(self.passes):
            new = self._pass(x, t0, seen)
            if not new:
                break
            found += new
            x = x.copy()
            for d in new:
                self.subtract(x, d)
        return found

    def _pass(self, x, t0, seen):
        cands = self.candidates(x, t0)
        if not cands:
            return []
        N = self.D * sfft.next_fast_len(-(-len(x) // self.D))
        X = sfft.rfft(x, n=N, workers=1)
        rows, info = [], []
        for c in cands:
            y, fc = self.baseband(X, N, c["freq"])
            n, df = self.refine(y, c["start"])
            Sy = self.symbols(y, n, df)
            rows.append(from_air(self.soft(Sy)))
            info.append((c, n, fc + df, Sy))
        rows = np.array(rows)
        bits, ok, used = bp_decode(rows, self.iters)
        # A strong signal gives each bit so much confidence that one symbol
        # hit by a fade or a burst of QRM can't be overruled (seen off the
        # air at +4 dB): try those that failed again with the confidence
        # capped. Each way decodes some the other doesn't.
        again = np.nonzero(~ok)[0]
        if len(again):
            b2, ok2, u2 = bp_decode(np.clip(rows[again], -self.LLR_MAX, self.LLR_MAX), self.iters)
            bits[again[ok2]], ok[again[ok2]], used[again[ok2]] = b2[ok2], True, u2[ok2]
        tried = 0
        for j in sorted(np.nonzero(~ok)[0], key=lambda j: -cands[j]["sync"]):
            if cands[j]["sync"] < self.OSD_SYNC or tried >= self.OSD_MAX:
                break
            tried += 1
            cw, dis = osd_decode(rows[j])
            if dis < self.OSD_DISCREPANCY * np.abs(rows[j]).sum() and \
                    bits_frame(cw[87:]) is not None:
                bits[j], ok[j], used[j] = cw, True, -1
        out = []
        for j in np.nonzero(ok)[0]:
            got = bits_frame(bits[j, 87:])
            if got is None:
                continue
            c, n, f, Sy = info[j]
            frame, i3 = got
            tn = tones(frame, i3, self.m)
            A2 = np.abs(Sy) ** 2
            hard = np.argmax(A2, 1)
            sync_ok = int((hard[self.sync_pos] == tn[self.sync_pos]).sum())
            nerr = int((bits[j] != (np.array(rows[j]) > 0)).sum())
            if sync_ok < 4 or nerr > 50:
                continue  # a codeword, but too unlike what was received
            key = (frame, i3)
            if key in seen:
                continue
            seen.add(key)
            ps = A2[np.arange(NSYM), tn]
            mask = np.ones_like(A2, bool)
            mask[np.arange(NSYM), tn] = False
            pn = float(np.median(A2[mask])) / np.log(2)
            snr = 10 * np.log10(max(ps.mean() / pn - 1, 1e-3)) + 10 * np.log10(self.m.baud / 2500)
            start = n * self.D / RATE + t0
            out.append({"frame": frame, "i3": i3, "freq": f, "dt": start - self.m.delay,
                        "snr": int(round(max(snr, -30))), "sync": c["sync"],
                        "submode": self.m.letter, "start": n * self.D, "iters": int(used[j]),
                        "errors": nerr})
        return out

    def subtract(self, x, d):
        """Take a decoded signal out of x: re-make it a symbol at a time with
        that symbol's measured amplitude and phase."""
        m = self.m
        tn = tones(d["frame"], d["i3"], m)
        s0 = int(d["start"])
        nn = np.arange(m.sps)
        for i, t in enumerate(tn):
            a = s0 + i * m.sps
            if a < 0 or a + m.sps > len(x):
                continue
            f = d["freq"] + t * m.baud
            e = np.exp(-2j * np.pi * f * (a + nn) / RATE)
            seg = x[a:a + m.sps]
            amp = 2 * np.dot(seg, e) / m.sps
            seg -= (amp * np.conj(e)).real.astype(np.float32)



# ------------------------------------------------------------- the frames

# The 3 transmission bits sent beside each frame.
FIRST, LAST, DATA = 1, 2, 4
# The first 3 of a frame's 72 bits say what it is; a data frame needs only
# its first bit, which is 1, and the second says whether it is compressed.
HEARTBEAT, COMPOUND, COMPOUND_DIRECTED, DIRECTED = 0, 1, 2, 3

CALL_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ /@"
NBASECALL = 37 * 36 * 10 * 27 * 27 * 27
NBASEGRID = 180 * 180
NUSERGRID = NBASEGRID + 10
NMAXGRID = (1 << 15) - 1

# Callsign values past NBASECALL name groups (protocol constants).
GROUPS = ("<....>", "@ALLCALL", "@JS8NET", "@DX/NA", "@DX/SA", "@DX/EU", "@DX/AS", "@DX/AF",
          "@DX/OC", "@DX/AN", "@REGION/1", "@REGION/2", "@REGION/3") + \
    tuple("@GROUP/%d" % i for i in range(10)) + \
    ("@COMMAND", "@CONTROL", "@NET", "@NTS") + tuple("@RESERVE/%d" % i for i in range(5)) + \
    ("@APRSIS", "@RAGCHEW", "@JS8", "@EMCOMM", "@ARES", "@MARS", "@AMRRON", "@RACES",
     "@RAYNET", "@RADAR", "@SKYWARN", "@CQ", "@HB", "@QSO", "@QSOPARTY", "@CONTEST",
     "@FIELDDAY", "@SOTA", "@IOTA", "@POTA", "@QRP", "@QRO")
GROUP_VALUE = {g: NBASECALL + 1 + i for i, g in enumerate(GROUPS)}
VALUE_GROUP = {v: g for g, v in GROUP_VALUE.items()}

# Directed commands by their 5-bit code, as JS8 writes them out.
COMMANDS = {0: " SNR?", 1: " DIT DIT", 2: " NACK", 3: " HEARING?", 4: " GRID?", 5: ">",
            6: " STATUS?", 7: " STATUS", 8: " HEARING", 9: " MSG", 10: " MSG TO:",
            11: " QUERY", 12: " QUERY MSGS", 13: " QUERY CALL", 14: " ACK", 15: " GRID",
            16: " INFO?", 17: " INFO", 18: " FB", 19: " HW CPY?", 20: " SK", 21: " RR",
            22: " QSL?", 23: " QSL", 24: " CMD", 25: " SNR", 26: " NO", 27: " YES", 28: " 73",
            29: " HEARTBEAT SNR", 30: " AGN?", 31: " "}
COMMAND_CODE = {v: k for k, v in COMMANDS.items()}
SNR_COMMANDS = (25, 29)
# Commands whose text comes after them in data frames, with a checksum of
# that text at the end: relay, MSG, MSG TO:, QUERY, QUERY MSGS, QUERY CALL,
# CMD. One whose checksum is wrong is dropped, as JS8 applications do.
CHECKSUMMED = (5, 9, 10, 11, 12, 13, 24)
CHECK_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-./?"
CQS = ("CQ CQ CQ", "CQ DX", "CQ QRP", "CQ CONTEST", "CQ FIELD", "CQ FD", "CQ CQ", "CQ")

# Uncompressed data frames: a fixed character code (a protocol constant).
HUFFMAN = {
    " ": "01", "E": "100", "T": "1101", "A": "0011", "O": "11111", "I": "11100", "N": "10111",
    "S": "10100", "H": "00011", "R": "00000", "D": "111011", "L": "110011", "C": "110001",
    "U": "101101", "M": "101011", "W": "001011", "F": "001001", "G": "000101", "Y": "000011",
    "P": "1111011", "B": "1111001", ".": "1110100", "V": "1100101", "K": "1100100",
    "-": "1100001", "+": "1100000", "?": "1011001", "!": "1011000", '"': "1010101",
    "X": "1010100", "0": "0010101", "J": "0010100", "1": "0010001", "Q": "0010000",
    "2": "0001001", "Z": "0001000", "3": "0000101", "5": "0000100", "4": "11110101",
    "9": "11110100", "8": "11110001", "6": "11110000", "7": "11101011", "/": "11101010"}
_HUFF_DECODE = {v: k for k, v in HUFFMAN.items()}


def frame_value(frame):
    v = 0
    for c in frame:
        v = (v << 6) | ALPHABET_INDEX[c]
    return v


def _field(v, start, n):
    """n bits of the 72-bit frame value v, from bit start (0 = first sent)."""
    return (v >> (72 - start - n)) & ((1 << n) - 1)


def unpack_call(value, portable=False):
    if value in VALUE_GROUP:
        return VALUE_GROUP[value]
    w = [""] * 6
    for i in (5, 4, 3):
        w[i] = CALL_CHARS[value % 27 + 10]
        value //= 27
    w[2] = CALL_CHARS[value % 10]
    value //= 10
    w[1] = CALL_CHARS[value % 36]
    value //= 36
    if value >= len(CALL_CHARS):
        return None
    w[0] = CALL_CHARS[value]
    call = "".join(w)
    if call.startswith("3D0"):
        call = "3DA0" + call[3:]
    if call[0] == "Q" and "A" <= call[1] <= "Z":
        call = "3X" + call[1:]
    call = call.strip()
    return call + "/P" if portable else call


def pack_call(call):
    """A callsign -> (28-bit value, portable), or None (for the test encoder)."""
    call = call.upper().strip()
    if call in GROUP_VALUE:
        return GROUP_VALUE[call], False
    portable = call.endswith("/P")
    if portable:
        call = call[:-2]
    if call.startswith("3DA0"):
        call = "3D0" + call[4:]
    if call.startswith("3X") and len(call) > 2 and call[2].isalpha():
        call = "Q" + call[2:]
    # Line it up as [digit or letter or space][digit or letter][digit][letters].
    for cand in (call, " " + call):
        cand = cand.ljust(6)
        if len(cand) == 6 and cand[2].isdigit() and cand[1] in CALL_CHARS[:36] and \
                cand[0] in CALL_CHARS[:37] and all(c == " " or c.isalpha() for c in cand[3:]):
            v = CALL_CHARS.index(cand[0])
            v = 36 * v + CALL_CHARS.index(cand[1])
            v = 10 * v + CALL_CHARS.index(cand[2])
            for c in cand[3:]:
                v = 27 * v + CALL_CHARS.index(c) - 10
            return v, portable
    return None


def unpack_alnum50(v):
    w = [""] * 11
    for i in (10, 9, 8):
        w[i] = CALL_CHARS[v % 38]
        v //= 38
    w[7] = "/" if v % 2 else " "
    v //= 2
    for i in (6, 5, 4):
        w[i] = CALL_CHARS[v % 38]
        v //= 38
    w[3] = "/" if v % 2 else " "
    v //= 2
    for i in (2, 1):
        w[i] = CALL_CHARS[v % 38]
        v //= 38
    w[0] = CALL_CHARS[v % 39]
    return "".join(w).replace(" ", "")


def pack_alnum50(text):
    w = "".join(c for c in text if c in CALL_CHARS)
    if len(w) > 3 and w[3] != "/":
        w = w[:3] + " " + w[3:]
    if len(w) > 7 and w[7] != "/":
        w = w[:7] + " " + w[7:]
    w = w.ljust(11)
    v = CALL_CHARS.index(w[0])
    for i in range(1, 11):
        if i in (3, 7):
            v = v * 2 + (w[i] == "/")
        else:
            v = v * 38 + CALL_CHARS.index(w[i])
    return v


def unpack_grid(value):
    """A 15-bit grid value -> a 4-character Maidenhead square ('' for none)."""
    if value > NBASEGRID:
        return ""
    lat = value % 180 - 90
    lon = value // 180 * 2 - 180 + 2
    nlon = int(60.0 * (180.0 - lon) / 5)
    nlat = int(60.0 * (lat + 90) / 2.5)
    return chr(65 + nlon // 240) + chr(65 + nlat // 240) + \
        chr(48 + (nlon % 240) // 24) + chr(48 + (nlat % 240) // 24)


def pack_grid(grid):
    if not grid or len(grid) < 4:
        return NMAXGRID
    g = grid[:4].upper()
    lon = 180 - 20 * (ord(g[0]) - 65) - 2 * (ord(g[2]) - 48) - 5 * 12.5 / 60
    lat = -90 + 10 * (ord(g[1]) - 65) + (ord(g[3]) - 48) + 2.5 * 12.5 / 60
    return ((int(lon) + 180) // 2) * 180 + int(lat + 90)


def crc16_kermit(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def checksum16(text):
    """The 3 characters a checksummed command's text ends with."""
    v = crc16_kermit(text.encode("latin-1", "replace"))
    n = len(CHECK_CHARS)
    return CHECK_CHARS[v // (n * n)] + CHECK_CHARS[v // n % n] + CHECK_CHARS[v % n]


def snr_text(snr):
    return ("+%02d" % snr) if snr >= 0 else ("-%02d" % -snr)


# The JSC word table, if the user has supplied one (see README): it is not
# part of rxd. None until loaded; False if there is none.
_JSC = None
JSC_PATHS = [os.path.expanduser(p) for p in (
    os.environ.get("RXD_JSC", ""), "~/.local/share/rxd/js8/jsc_map.txt",
    "/usr/local/share/rxd/jsc_map.txt") if p]


def jsc_table():
    global _JSC
    if _JSC is None:
        _JSC = False
        for p in JSC_PATHS:
            try:
                with open(p, encoding="ascii") as fh:
                    words = [w.encode("ascii").decode("unicode_escape")
                             for w in fh.read().split("\n")]
            except (OSError, ValueError):
                continue
            if words and words[-1] == "":
                words.pop()
            if len(words) >= 1000:
                _JSC = words
                break
    return _JSC or None


def jsc_decompress(bits, table):
    """(s,c)-dense code with 4-bit digits, s=7, c=9 (Brisaboa et al.): each
    word is any number of continuers (8-15) and one stopper (0-7); a bit
    after each stopper says whether a space follows the word."""
    s, c = 7, 9
    base = [0]
    for k in range(1, 8):
        base.append(base[-1] + s * c ** (k - 1))
    digits, spaces = [], set()
    i = 0
    while i + 4 <= len(bits):
        d = (bits[i] << 3) | (bits[i + 1] << 2) | (bits[i + 2] << 1) | bits[i + 3]
        digits.append(d)
        i += 4
        if d < s:
            if i < len(bits) and bits[i]:
                spaces.add(len(digits) - 1)
            i += 1
    out, start = [], 0
    while start < len(digits):
        j = k = 0
        while start + k < len(digits) and digits[start + k] >= s:
            j = j * c + digits[start + k] - s
            k += 1
        if start + k >= len(digits) or k >= len(base):
            break
        j = j * s + digits[start + k] + base[k]
        if j >= len(table):
            break
        out.append(table[j])
        if start + k in spaces:
            out.append(" ")
        start += k + 1
    return "".join(out)


def _bits72(v):
    return [(v >> (71 - i)) & 1 for i in range(72)]


def _huffman(bits):
    out, code = [], ""
    for b in bits:
        code += "1" if b else "0"
        if code in _HUFF_DECODE:
            out.append(_HUFF_DECODE[code])
            code = ""
        elif len(code) > 8:
            break
    return "".join(out)


def unpack(frame, i3):
    """One frame -> a dict: kind (heartbeat, cq, compound, directed, data or
    raw), text as JS8 shows it, and the parts. Data that needs the JSC
    table and doesn't have it comes back as kind "jsc" with text None."""
    v = frame_value(frame)
    flag = _field(v, 0, 3)
    tail8 = _field(v, 64, 8)
    if i3 & DATA:
        b = _bits72(v)
        n = len(b) - 1 - b[::-1].index(0) if 0 in b else 0
        table = jsc_table()
        if table is None:
            return {"kind": "jsc", "text": None}
        text = jsc_decompress(b[:n], table)
        return {"kind": "data", "text": text} if text else {"kind": "raw", "text": frame}
    if flag >= 4:
        b = _bits72(v)[1:]
        compressed = b[0]
        n = len(b) - 1 - b[::-1].index(0) if 0 in b else 0
        payload = b[1:n]
        if compressed:
            table = jsc_table()
            if table is None:
                return {"kind": "jsc", "text": None}
            text = jsc_decompress(payload, table)
        else:
            text = _huffman(payload)
        if text:
            return {"kind": "data", "text": text}
    if flag != DIRECTED and flag != 4:
        call = unpack_alnum50(_field(v, 3, 50))
        num = (_field(v, 53, 11) << 5) | (tail8 >> 3)
        bits3 = tail8 & 7
        if flag == HEARTBEAT:
            grid = unpack_grid(num & 0x7FFF)
            if num & 0x8000:
                kind, what = "cq", "@ALLCALL " + CQS[bits3]
            else:
                kind, what = "heartbeat", "@HB HEARTBEAT"
            return {"kind": kind, "from": call, "to": what.split()[0], "cmd": what[what.index(" "):],
                    "grid": grid, "text": "%s: %s %s " % (call, what, grid)}
        if flag in (COMPOUND, COMPOUND_DIRECTED):
            extra, cmd, grid = [], None, ""
            if num <= NBASEGRID:
                grid = unpack_grid(num)
                extra.append(" " + grid)
            elif NUSERGRID <= num < NMAXGRID:
                code, n = _unpack_cmd(num - NUSERGRID)
                cmd = COMMANDS.get(code)
                if cmd is not None:
                    extra.append(cmd)
                    if code in SNR_COMMANDS:
                        extra.append(snr_text(n - 31))
            if flag == COMPOUND:
                return {"kind": "compound", "from": call, "grid": grid, "text": call + ": "}
            return {"kind": "compound_directed", "to": call, "cmd": cmd, "grid": grid,
                    "extra": extra[1:] if cmd else [], "text": call + " ".join(extra) + " "}
        return {"kind": "raw", "text": frame}
    if flag == DIRECTED:
        frm = unpack_call(_field(v, 3, 28), (tail8 >> 7) & 1)
        to = unpack_call(_field(v, 31, 28), (tail8 >> 6) & 1)
        code = _field(v, 59, 5) % 32
        cmd = COMMANDS[code]
        num = tail8 % 64
        if frm is None or to is None:
            return {"kind": "raw", "text": frame}
        extra = []
        if num:
            extra.append(snr_text(num - 31) if code in SNR_COMMANDS else str(num - 31))
        return {"kind": "directed", "from": frm, "to": to, "cmd": cmd, "extra": extra,
                "text": "%s: %s%s " % (frm, to, " ".join([cmd] + extra))}
    return {"kind": "raw", "text": frame}


def _unpack_cmd(value):
    if value & 0x80:
        return (29 if value & 0x40 else 25), value & 0x3F
    return value & 0x7F, 0


def pack_directed(frm, to, cmd, num=None, i3=FIRST | LAST):
    """A directed frame (for tests and the test encoder)."""
    f, fp = pack_call(frm)
    t, tp = pack_call(to)
    code = COMMAND_CODE[cmd]
    extra = 0 if num is None else max(-30, min(num, 31)) + 31
    v = (DIRECTED << 69) | (f << 41) | (t << 13) | (code << 8) | (fp << 7) | (tp << 6) | extra
    return value_frame(v), i3


def pack_heartbeat(call, grid="", cq=None, i3=FIRST | LAST):
    num = pack_grid(grid) if grid else NMAXGRID
    bits3 = 0
    if cq is not None:
        num |= 0x8000
        bits3 = CQS.index(cq)
    v = (HEARTBEAT << 69) | (pack_alnum50(call) << 19) | ((num >> 5) << 8) | ((num & 31) << 3) | bits3
    return value_frame(v), i3


def pack_huffman(text, i3=0):
    """An uncompressed data frame holding as much of text as fits."""
    bits = [1, 0]
    for ch in text.upper():
        code = HUFFMAN.get(ch)
        if code is None or len(bits) + len(code) >= 72:
            break
        bits += [int(c) for c in code]
    bits += [0] + [1] * (71 - len(bits))
    v = 0
    for b in bits[:72]:
        v = (v << 1) | b
    return value_frame(v), i3


def value_frame(v):
    return "".join(ALPHABET[(v >> (66 - 6 * i)) & 63] for i in range(12))


# --------------------------------------------------------- the messages

EOT = "♢"   # JS8's end-of-message mark
GAP = "……"


class Assembler:
    """Joins frames into messages, as written to DIRECTED.TXT.

    A message is the frames one station sends at one offset, slot after slot,
    from a frame flagged first to one flagged last. Heartbeats, CQs and
    directed commands start one; data frames continue it. A missing frame
    shows as '……', and a message that stops without its last frame is let go
    after a few slots. feed(slot, decodes) returns the finished messages as
    dicts: from, to, text (in JS8's usual form), freq, snr, submode, slot."""

    TOLERANCE = 10.0   # Hz between frames of one message
    LINGER = 3         # slots without a frame before an unfinished message goes

    def __init__(self):
        self.open = []

    def feed(self, slot, decodes, slot_len=15, submode=None):
        done = []
        for d in sorted(decodes, key=lambda d: d["freq"]):
            u = d.get("unpacked") or unpack(d["frame"], d["i3"])
            i3 = d["i3"]
            kind = u["kind"]
            msg = self._near(d["freq"], d["submode"])
            starts = kind in ("heartbeat", "cq", "directed", "compound", "compound_directed")
            if starts and (i3 & FIRST or msg is None):
                if msg is not None and not (kind == "directed" and msg["head"] is not None and
                                            msg["head"].get("kind") == "compound"):
                    done.append(self._close(msg, complete=False))
                    msg = None
                if msg is None:
                    msg = self._new(d, slot)
                self._header(msg, u)
            elif msg is None:
                msg = self._new(d, slot)
                msg["parts"].append(GAP)
                self._data(msg, u, d)
            else:
                self._step(msg, slot, slot_len)
                if starts:
                    self._header(msg, u)
                else:
                    self._data(msg, u, d)
            msg["last_slot"] = slot
            msg["snr"] = d["snr"]
            if i3 & LAST:
                done.append(self._close(msg, complete=True))
        for msg in list(self.open):
            if submode is not None and msg["submode"] != submode:
                continue
            if slot - msg["last_slot"] >= self.LINGER * slot_len:
                done.append(self._close(msg, complete=False))
        return [x for x in done if x]

    def flush(self):
        out = [self._close(m, complete=False) for m in list(self.open)]
        return [x for x in out if x]

    def _near(self, f, sub):
        best = None
        for m in self.open:
            if m["submode"] == sub and abs(m["freq"] - f) <= self.TOLERANCE:
                if best is None or abs(m["freq"] - f) < abs(best["freq"] - f):
                    best = m
        return best

    def _new(self, d, slot):
        m = {"freq": d["freq"], "submode": d["submode"], "slot": slot, "last_slot": slot,
             "head": None, "parts": [], "snr": d["snr"], "jsc": False}
        self.open.append(m)
        return m

    def _step(self, msg, slot, slot_len):
        if slot - msg["last_slot"] > slot_len and (not msg["parts"] or msg["parts"][-1] != GAP):
            msg["parts"].append(GAP)

    def _header(self, msg, u):
        head = msg["head"]
        if head is not None and head.get("kind") == "compound":
            # A compound callsign's frame, then the frame it is the sender of.
            if u["kind"] == "directed" and u["from"] == "<....>":
                u = dict(u, **{"from": head["from"]})
            elif u["kind"] == "compound_directed":
                u = dict(u, **{"from": head["from"], "kind": "directed",
                               "extra": u.get("extra", [])})
        elif head is not None and head.get("kind") == "directed" and u["kind"] == "compound_directed":
            # A directed frame to "<....>", then the compound callsign it's to.
            u = dict(head, to=u["to"], cmd=u.get("cmd") or head["cmd"],
                     extra=u.get("extra") or head.get("extra", []))
        msg["head"] = u

    def _data(self, msg, u, d):
        if u["kind"] == "jsc":
            msg["jsc"] = True
            msg["parts"].append(GAP)
        elif u["kind"] == "data":
            if msg["parts"] and msg["parts"][-1] != GAP:
                msg["parts"][-1] += u["text"]
            else:
                msg["parts"].append(u["text"])
        else:
            msg["parts"].append(u["text"].strip())

    def _close(self, msg, complete):
        if msg in self.open:
            self.open.remove(msg)
        h = msg["head"]
        body = " ".join(p for p in msg["parts"]).strip()
        if h is None:
            if not body or body == GAP:
                return None
            frm, _, _ = body.partition(":")
            text, frm, to = body, (frm if ":" in body else None), None
        elif h["kind"] in ("heartbeat", "cq"):
            text = "%s: %s%s" % (h["from"], h["to"], h["cmd"])
            if h.get("grid"):
                text += " " + h["grid"]
            frm, to = h["from"], h["to"]
        elif h["kind"] == "compound":
            text = h["text"].strip() + (" " + h["grid"] if h.get("grid") else "")
            frm, to = h["from"], None
        elif h["kind"] == "compound_directed":
            text = "<....>: %s%s" % (h["to"], " ".join([h.get("cmd") or ""] + h.get("extra", [])))
            frm, to = None, h["to"]
        else:
            text = "%s: %s%s" % (h["from"], h["to"], " ".join([h["cmd"]] + h.get("extra", [])))
            frm, to = h["from"], h["to"]
        code = COMMAND_CODE.get(h.get("cmd")) if h is not None else None
        if code in CHECKSUMMED and h["kind"] == "directed":
            # The text is the data frames run together; it ends in a space
            # and its checksum, which is checked and taken off.
            whole = "".join(p for p in msg["parts"] if p != GAP).strip()
            if not complete or GAP in msg["parts"] or len(whole) < 4 or \
                    checksum16(whole[:-4]) != whole[-3:]:
                return None
            body = whole[:-4]
        if h is not None and body:
            text = text.rstrip() + " " + body if h.get("cmd") != " " else text + " " + body
        text = text.rstrip()
        if complete:
            text += " " + EOT
        return {"from": frm, "to": to, "text": text, "freq": msg["freq"], "snr": msg["snr"],
                "submode": msg["submode"], "slot": msg["slot"], "complete": complete,
                "needs_jsc": msg["jsc"]}


# ----------------------------------------------------------- the records

def all_line(slot, d, text):
    """A line in JS8's usual ALL.TXT form."""
    return "%s%3d %4.1f %4d %s  %-12s         %d   %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(slot)), d["snr"], d["dt"],
        int(round(d["freq"])), d["submode"], d["frame"], d["i3"], text if text is not None else "")


def directed_line(when, dial, msg):
    """A line in JS8's usual DIRECTED.TXT form (the offset is the sender's)."""
    return "%s\t%.6f\t%d\t%s\t%s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(when)),
                                     dial / 1e6, int(round(msg["freq"])), snr_text(msg["snr"]),
                                     msg["text"])


def decode_slot(x, t0, submodes=("normal",), decoders=None):
    """Every submode's decodes in one block of audio, each with its frame
    unpacked. decoders caches Decoder objects between calls."""
    decoders = {} if decoders is None else decoders
    out = []
    for name in submodes:
        dec = decoders.get(name) or decoders.setdefault(name, Decoder(name))
        for d in dec.decode(x, t0):
            d["unpacked"] = unpack(d["frame"], d["i3"])
            out.append(d)
    return out


# -------------------------------------------------------- serving rxd

def serve(inp=None, out=None):
    """Decode slots for rxd: a JSON header line, then the audio as float32;
    one JSON line back per slot. Runs at reduced priority, in its own process,
    so a busy band can't starve rxd's audio."""
    inp = inp or sys.stdin.buffer
    out = out or sys.stdout
    try:
        os.nice(10)
    except OSError:
        pass
    decoders = {}
    jsc_table()
    while True:
        line = inp.readline()
        if not line:
            return 0
        job = json.loads(line)
        raw = inp.read(4 * job["n"])
        if len(raw) < 4 * job["n"]:
            return 1
        x = np.frombuffer(raw, "<f4")
        c0, w0 = time.process_time(), time.monotonic()
        try:
            res = decode_slot(x, job["t0"], [job["submode"]], decoders)
            reply = {"id": job.get("id"), "decodes": res, "error": None}
        except Exception as e:  # noqa: BLE001 - reported to rxd, not fatal
            reply = {"id": job.get("id"), "decodes": [], "error": "%s: %s" % (type(e).__name__, e)}
        reply["cpu"] = time.process_time() - c0
        reply["wall"] = time.monotonic() - w0
        out.write(json.dumps(reply, default=float) + "\n")
        out.flush()


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError("%s: 16-bit WAV only" % path)
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    if rate != RATE:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(rate, RATE)
        x = resample_poly(x, RATE // g, rate // g).astype(np.float32)
    return x


def write_wav(path, x, rate=RATE):
    import wave
    pcm = np.clip(np.asarray(x) * 32767, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ------------------------------------------------------- inside rxd

JS8_DIR = os.path.expanduser(os.environ.get("RXD_JS8_DIR", "~/.local/share/rxd/js8"))


class Resampler:
    """48 kHz (or any multiple of 12 kHz) -> 12 kHz, block by block."""

    def __init__(self, rate_in):
        from scipy import signal as sps
        if rate_in % RATE:
            raise ValueError("audio must come at a multiple of 12 kHz")
        self.n = rate_in // RATE
        self.h = sps.firwin(129, 5000, fs=rate_in).astype(np.float32)
        self.zi = np.zeros(len(self.h) - 1, np.float32)
        self.phase = 0
        self.lfilter = sps.lfilter

    def __call__(self, x):
        if self.n == 1:
            return np.asarray(x, np.float32)
        y, self.zi = self.lfilter(self.h, 1.0, np.asarray(x, np.float32), zi=self.zi)
        out = y[self.phase::self.n]
        self.phase = (self.phase - len(y)) % self.n
        return out.astype(np.float32)


class SlotDecoder:
    """rxd's audio -> slots by the system clock -> the decoder process ->
    ALL.TXT and DIRECTED.TXT under datadir, in JS8's usual log forms.

    push() is called from rxd's DSP thread with the audio before squelch and
    volume; it only copies into a ring and hands finished slots to a worker
    thread, which feeds a separate, lower-priority decoder process. A slot is
    dropped when the dial moves during it or the audio has a gap."""

    RING_S = 45
    KEEP_BYTES = 8 << 20   # each log is rolled over to .1 past this

    def __init__(self, dial_hz=None, submodes=("normal",), datadir=None, rate_in=48000,
                 clock=time.time, inline=False):
        self.submodes = [s for s in submodes if s in SUBMODES]
        if not self.submodes:
            raise ValueError("no JS8 submodes among %s" % (submodes,))
        self.dir = datadir or JS8_DIR
        os.makedirs(self.dir, exist_ok=True)
        self.directed_path = os.path.join(self.dir, "DIRECTED.TXT")
        self.all_path = os.path.join(self.dir, "ALL.TXT")
        self.clock = clock
        self.inline = inline
        self.resample = Resampler(rate_in)
        self.ring = np.zeros(int(self.RING_S * RATE), np.float32)
        self.written = 0          # samples ever written
        self.valid_from = 0       # first sample since the last gap or retune
        self.t_end = None         # the clock when the last sample arrived
        self.dial = dial_hz
        self.windows = {s: Decoder(s).window() for s in self.submodes}
        self.next_slot = {}
        import queue
        import threading
        self.jobs = queue.Queue(maxsize=2 * len(self.submodes))
        self.lock = threading.Lock()
        self.assembler = Assembler()
        self.proc = None
        self.decoders = {}
        self.decoding = None
        self.last = None
        self.error = None
        self.skipped = 0
        self.cpu = []             # (cpu seconds, slot seconds) for recent slots
        self.worker = threading.Thread(target=self._work, name="js8", daemon=True)
        self.worker.start()

    # ----------------------------------------------------- recording
    def push(self, audio, dial, now=None):
        a = self.resample(audio)
        now = self.clock() if now is None else now
        if self.t_end is not None:
            gap = now - self.t_end - len(a) / RATE
            if dial != self.dial or abs(gap) > 0.5:
                self.valid_from = self.written  # retuned, or the audio stalled
        self.dial = dial
        n = len(a)
        if n >= len(self.ring):
            a, n = a[-len(self.ring):], len(self.ring)
        i = self.written % len(self.ring)
        k = min(n, len(self.ring) - i)
        self.ring[i:i + k] = a[:k]
        self.ring[:n - k] = a[k:]
        self.written += n
        self.t_end = now
        for name in self.submodes:
            self._due(name)

    def _due(self, name):
        m = SUBMODES[name]
        before, after = self.windows[name]
        if name not in self.next_slot:
            self.next_slot[name] = (self.t_end // m.slot) * m.slot
        s = self.next_slot[name]
        if self.t_end < s + m.slot + after:
            return
        self.next_slot[name] = s + m.slot
        if self.t_end > s + 2 * m.slot + after:  # far behind (a stall): skip ahead
            self.next_slot[name] = (self.t_end // m.slot) * m.slot
        length = int(round((m.slot + before + after) * RATE))
        first = self.written - int(round((self.t_end - (s - before)) * RATE))
        if first < self.valid_from or first < self.written - len(self.ring) or first < 0:
            return  # joined late, retuned, or a gap: not a whole slot
        idx = np.arange(first, first + length) % len(self.ring)
        try:
            self.jobs.put_nowait((name, s, -before, self.ring[idx].copy(), self.dial))
        except Exception:  # queue.Full
            self.skipped += 1
            self.error = "decoding is falling behind; a slot was skipped"

    # ----------------------------------------------------- decoding
    def _work(self):
        while True:
            name, slot, t0, x, dial = self.jobs.get()
            if name is None:
                return
            self.decoding = (name, slot)
            try:
                decodes, cpu, wall = self._decode(name, x, t0)
                self.error = None
            except (OSError, ValueError, RuntimeError) as e:
                self.error = "JS8 decoder failed: %s" % e
                decodes, cpu, wall = [], 0.0, 0.0
            finally:
                self.decoding = None
            self._record(name, slot, dial, decodes, cpu, wall)

    def _decode(self, name, x, t0):
        if self.inline:
            c0, w0 = time.process_time(), time.monotonic()
            d = decode_slot(x, t0, [name], self.decoders)
            return d, time.process_time() - c0, time.monotonic() - w0
        import subprocess
        for attempt in (1, 2):
            if self.proc is None or self.proc.poll() is not None:
                self.proc = subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--serve"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                head = json.dumps({"submode": name, "t0": t0, "n": len(x)}) + "\n"
                self.proc.stdin.write(head.encode() + np.asarray(x, "<f4").tobytes())
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if not line:
                    raise OSError("the decoder process exited")
                r = json.loads(line)
                if r.get("error"):
                    raise RuntimeError(r["error"])
                return r["decodes"], r["cpu"], r["wall"]
            except (OSError, ValueError) as e:
                self.proc.kill()
                self.proc = None
                if attempt == 2:
                    raise OSError(str(e))
        return [], 0.0, 0.0

    def _record(self, name, slot, dial, decodes, cpu, wall):
        m = SUBMODES[name]
        msgs = self.assembler.feed(slot, decodes, m.slot, m.letter)
        now = self.clock()
        with self.lock:
            self.cpu = (self.cpu + [(cpu, m.slot)])[-40:]
            self.last = {"slot": iso(slot), "submode": name, "decodes": len(decodes),
                         "messages": len(msgs), "cpu_s": round(cpu, 2), "wall_s": round(wall, 2)}
        self._append(self.all_path, [all_line(slot, d, d["unpacked"]["text"]) for d in decodes])
        if dial is not None:
            self._append(self.directed_path, [directed_line(now, dial, x) for x in msgs])

    def _append(self, path, rows):
        if not rows:
            return
        try:
            if os.path.getsize(path) > self.KEEP_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(r + "\n" for r in rows))
        except OSError as e:
            self.error = "can't write %s: %s" % (path, e)

    # ------------------------------------------------------- status
    def status(self):
        now = self.clock() if self.t_end is None else self.t_end
        with self.lock:
            cpu = sum(c for c, _ in self.cpu)
            span = sum(s for _, s in self.cpu) / max(len(self.submodes), 1)
            if self.decoding is not None:
                state, slot = "decoding", self.decoding[1]
            elif self.t_end is None:
                state, slot = "waiting", None
            else:
                state = "recording"
                slot = self.next_slot.get(self.submodes[0])
            return {"engine": "rxd", "version": VERSION, "submodes": self.submodes,
                    "state": state, "slot": iso(slot) if slot is not None else None,
                    "last": self.last, "error": self.error, "skipped": self.skipped,
                    "cpu_pct": round(100 * cpu / span, 1) if span else None,
                    "jsc": jsc_path() is not None, "log": self.directed_path}

    def close(self):
        self.jobs.put((None, 0, 0, None, None))
        if self.proc is not None:
            self.proc.kill()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def jsc_path():
    for p in JSC_PATHS:
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------- the JSC word table

JSC_WORDS = 262144
# The table's source: JS8Call's jsc_map.cpp (GPL-3, (C) 2018 Jordan Sherer),
# so it is never in rxd; install.py fetches it and this importer, rxd's own
# code, turns it into the word list rxd reads. Pinned to the commit
# of JS8Call v2.2.0-ga in the original repository. The same table, entry for
# entry, is in that repository's later main and in JS8Call-improved v3.0.3
# (JS8_JSC/JSC_map.cpp), whose file is laid out differently.
JSC_SOURCE_URL = ("https://raw.githubusercontent.com/js8call/js8call/"
                  "99018054a3b273f7086f4e086f5b848c645c23c8/jsc_map.cpp")
JSC_SOURCE_BYTES = 6296397
JSC_SOURCE_SHA256 = "63dcea037badcdc49c5fc29a23438b66953fbfefcc1e354331f58c7dec232927"
_JSC_ENTRY = None


def jsc_table_path():
    return os.path.join(JS8_DIR, "jsc_map.txt")


def jsc_installed_sha256(dest=None):
    """The SHA-256 of the source the installed table was built from, or None."""
    try:
        with open((dest or jsc_table_path()) + ".sha256", encoding="ascii") as fh:
            return fh.read().split()[0]
    except (OSError, IndexError):
        return None


def jsc_import(src, dest=None, sha256=JSC_SOURCE_SHA256, words=JSC_WORDS):
    """Write the JSC word table, one word per line in code order (escaped as
    Python writes a string's insides, since one word is a newline), from a
    copy of jsc_map.cpp checked against sha256, and beside it the source's
    SHA-256 (DEST.sha256). Nothing is replaced unless all of it succeeds, so
    a bad or partial download leaves the table already there. Returns (dest,
    word count)."""
    import hashlib
    dest = dest or jsc_table_path()
    with open(src, "rb") as fh:
        data = fh.read()
    digest = hashlib.sha256(data).hexdigest()
    if sha256 is not None and digest != sha256:
        raise ValueError("%s is not the pinned jsc_map.cpp (sha256 %s, want %s)"
                         % (src, digest, sha256))
    table = jsc_parse(data.decode("latin-1"), words)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for path, body in ((dest, "".join(w.encode("unicode_escape").decode("ascii") + "\n"
                                      for w in table)),
                       (dest + ".sha256", digest + "  " + os.path.basename(src) + "\n")):
        tmp = path + ".new"
        with open(tmp, "w", encoding="ascii", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
    global _JSC
    _JSC = None   # this process reads it at its next look; rxd at its next start
    return dest, len(table)


def jsc_parse(text, words=JSC_WORDS):
    """The words of jsc_map.cpp's JSC::map array, by index: entries
    {"word", size, index}, a C string literal, sometimes with a comment."""
    import re
    global _JSC_ENTRY
    _JSC_ENTRY = _JSC_ENTRY or re.compile(
        r'\{\s*"((?:[^"\\]|\\.)*)"\s*(?:/\*.*?\*/\s*)?,\s*\d+\s*,\s*(\d+)\s*\}')
    start = text.index("JSC::map[")
    body = text[start:text.index("};", start)]
    n = words
    words = [None] * n
    for m in _JSC_ENTRY.finditer(body):
        i = int(m.group(2))
        if i < n:
            words[i] = _c_unescape(m.group(1))
    if None in words:
        raise ValueError("jsc_map.cpp: %d of %d words missing" % (words.count(None), n))
    return words


def _c_unescape(s):
    simple = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "a": "\a",
              "b": "\b", "f": "\f", "v": "\v", "?": "?"}
    out, i = [], 0
    while i < len(s):
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        n = s[i + 1]
        if n == "x":
            j = i + 2
            while j < len(s) and s[j] in "0123456789abcdefABCDEF":
                j += 1
            out.append(chr(int(s[i + 2:j], 16) & 0xFF))
            i = j
        elif n in "01234567":
            j = i + 1
            while j < len(s) and j < i + 4 and s[j] in "01234567":
                j += 1
            out.append(chr(int(s[i + 1:j], 8) & 0xFF))
            i = j
        else:
            out.append(simple.get(n, n))
            i += 2
    return "".join(out)

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="js8", description="rxd's JS8 decoder")
    ap.add_argument("--serve", action="store_true", help="decode slots for rxd on stdin/stdout")
    ap.add_argument("--wav", nargs="+", help="decode WAV files, each one slot")
    ap.add_argument("--t0", type=float, default=0.0,
                    help="where the WAV starts, in seconds from the slot's start")
    ap.add_argument("--submode", default="normal", help="normal, fast, turbo, slow or all")
    ap.add_argument("--jsc-import-source", metavar="FILE",
                    help="build the JSC word table from JS8Call's jsc_map.cpp "
                    "(JSC_SOURCE_URL, checked against JSC_SOURCE_SHA256)")
    ap.add_argument("--jsc-pin", action="store_true",
                    help="print the pinned source's URL, SHA-256, and whether the "
                    "installed table was built from it")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(VERSION)
        return 0
    if a.serve:
        return serve()
    if a.jsc_pin:
        print(JSC_SOURCE_URL)
        print(JSC_SOURCE_SHA256)
        print("installed" if jsc_installed_sha256() == JSC_SOURCE_SHA256 else "missing")
        return 0
    if a.jsc_import_source:
        try:
            dest, n = jsc_import(a.jsc_import_source)
        except (OSError, ValueError) as e:
            print("js8: %s; the table already there is kept" % e, file=sys.stderr)
            return 1
        print("wrote %d JSC words to %s" % (n, dest))
        return 0
    if a.wav:
        subs = list(SUBMODES) if a.submode == "all" else a.submode.split(",")
        decoders = {}
        for path in a.wav:
            x = read_wav(path)
            for d in decode_slot(x, a.t0, subs, decoders):
                print(all_line(0, d, d["unpacked"]["text"]))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
