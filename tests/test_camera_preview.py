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
    assert 'if(liveRef.current)return' in src
    assert 'Date.now()-lastAttemptRef.current<15000' in src
