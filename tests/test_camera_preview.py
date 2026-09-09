"""Camera preview transport regression tests.

The panel has to show a live picture in every browser that can run on a Linux
desktop — including Firefox on Ubuntu/Wayland, which ships without an H.264
decoder. These tests pin the two failures that made a camera card stay black or
reload forever:

1. the WebRTC offer was created without a transceiver, so go2rtc answered with
   an SDP without media and no track ever arrived;
2. the periodic health check read a stale `live` flag and restarted the
   transport every 10s, tearing down a working MJPEG fallback.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend' / 'src' / 'main.tsx'


def test_webrtc_offer_requests_video_media():
    src = FRONTEND.read_text()
    # go2rtc can only answer an offer that asks for media.
    assert "addTransceiver('video'" in src
    assert "direction:'recvonly'" in src
    # Unified plan + max-bundle, like go2rtc's own web client.
    assert "sdpSemantics:'unified-plan'" in src
    assert "bundlePolicy:'max-bundle'" in src
    # Media must be attached from the transceivers, not only from ontrack.
    assert 'pc.getTransceivers()' in src


def test_browsers_without_h264_get_vp8_and_mjpeg_fallback():
    src = FRONTEND.read_text()
    # Codec negotiation: keep only VP8/VP9 when H.264 cannot be decoded.
    assert 'applyVideoCodecPreference' in src
    assert 'setCodecPreferences' in src
    assert 'function canDecodeH264()' in src
    # MSE/HLS carry H.264 only: those transports must not be used there.
    assert 'if(!canDecodeH264()){beginMjpeg();return}' in src
    assert 'GO2RTC_VIDEO_CODECS' in src
    # go2rtc needs the codec list to start an MSE stream at all.
    assert "type:'mse',value:mseCodecs()" in src


def test_health_check_uses_fresh_liveness_and_cooldown():
    src = FRONTEND.read_text()
    # A stale `live` closure used to restart the transport on every tick.
    assert 'liveRef' in src
    assert 'lastAttemptRef' in src
    # A transport only counts as healthy once it has actually PAINTED a frame.
    # Checking `liveRef` alone let a connected-but-not-decoding stream (wrong
    # codec, broken variant) block every retry, which is the black-card bug.
    assert 'if(liveRef.current&&paintedRef.current)return' in src
    assert 'Date.now()-lastAttemptRef.current<15000' in src


def test_transport_is_live_only_after_a_frame_is_decoded():
    """Signalling success must never be mistaken for a visible picture.

    WebRTC used to go live on the first track and MSE on the first appended
    segment. A track carrying a codec the browser cannot decode satisfies both
    conditions while painting nothing, so the card stayed black *and* the
    health check refused to re-arm it. Every transport now has to prove the
    <video> element is really decoding before it is trusted.
    """
    src = FRONTEND.read_text()
    assert 'verifyPaintingRef' in src
    assert 'paintedRef' in src
    # Decoding evidence: readyState plus advancing time or decoded frame count.
    assert 'getVideoPlaybackQuality' in src
    assert 'v.readyState>=2' in src
    # The snapshot underlay must survive a connected-but-blank transport.
    assert 'if(live&&painted){setStillSrc' in src


def test_preview_quality_ladder_falls_back_to_the_native_stream():
    """A scaled variant must never be the only stream the card can play.

    The Vesktop-style quality ladder registers a separate `zmk-{id}-q` stream.
    If that ffmpeg transcode is missing or fails, the cascade has to fall back
    to the untouched feed instead of leaving an empty card.
    """
    src = FRONTEND.read_text()
    assert 'previewStreamNames' in src
    assert '`zmk-${id}-q`' in src
    # Native feed and the legacy plain id stay in the candidate list.
    assert "[`zmk-${id}-q`,`zmk-${id}`,id]" in src
    assert "[`zmk-${id}`,id]" in src
