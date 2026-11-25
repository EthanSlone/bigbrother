from bigbrother.probe import StreamReport, analyze_packets, recommendations, render_markdown


def packets(fps=15, seconds=10, gop_frames=30, size=5000):
    out = []
    for i in range(fps * seconds):
        out.append({
            "pts_time": f"{i / fps:.6f}",
            "flags": "K__" if i % gop_frames == 0 else "___",
            "size": str(size),
        })
    return out


def test_analyze_packets():
    fps, gop, kbps = analyze_packets(packets(fps=15, gop_frames=30, size=5000))
    assert abs(fps - 15) < 0.2
    assert gop == 2.0
    assert 590 < kbps < 610  # 15 fps * 5000 B * 8 = 600 kbps


def test_analyze_packets_untimed_first_keyframe():
    """RTSP sessions often start with a keyframe that has no timestamp."""
    pk = packets(fps=15, seconds=8, gop_frames=60)
    pk[0]["pts_time"] = "N/A"
    fps, gop, _ = analyze_packets(pk)
    assert gop == 4.0


def test_analyze_packets_empty():
    assert analyze_packets([]) == (None, None, None)


def test_recommendations_flag_long_gop_and_low_res_main():
    r = StreamReport(camera="a", stream="main", url="-", ok=True, video_codec="hevc",
                     width=1920, height=1080, keyframe_interval_s=4.0)
    notes = " ".join(recommendations(r))
    assert "Keyframe every 4.0s" in notes
    assert "hvc1" in notes
    assert "native resolution" in notes


def test_render_markdown_includes_errors_and_storage_estimate():
    reports = [
        StreamReport(camera="a", stream="main", url="-", ok=True, video_codec="h264",
                     width=2560, height=1440, measured_fps=15, declared_fps=15,
                     keyframe_interval_s=1.0, bitrate_kbps=4000),
        StreamReport(camera="a", stream="sub", url="-", error="Authentication failed"),
    ]
    md = render_markdown(reports)
    assert "2560x1440" in md
    assert "Authentication failed" in md
    assert "43 GB/day" in md  # 4000 kbps -> 43.2 GB/day
