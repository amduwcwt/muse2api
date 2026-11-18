"""Noise 帧按 UTF-8 字节切开。不启动浏览器，不访问网络。

运行：python tests/test_noise_frames.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from config import max_prompt_bytes, max_prompt_chars  # noqa: E402
from engine import (  # noqa: E402
    NOISE_FRAME_PLAINTEXT_BYTES,
    NOISE_MAX_FRAME_BYTES,
    MuseEngine,
    MuseGenerationError,
    MusePromptTooLargeError,
    ensure_prompt_within_cap,
    prompt_nbytes,
    split_utf8_frames,
)

EMOJI = "😀"  # U+1F600, 4 bytes
CJK = "你"    # U+4F60, 3 bytes


def _assert_frames(text: str, frames: list[str], limit: int) -> None:
    assert NOISE_FRAME_PLAINTEXT_BYTES <= NOISE_MAX_FRAME_BYTES
    assert limit <= NOISE_MAX_FRAME_BYTES
    raws = [frame.encode("utf-8") for frame in frames]
    for raw in raws:
        assert 0 < len(raw) <= limit or (text == "" and raw == b"")
        assert len(raw) <= NOISE_MAX_FRAME_BYTES
        if raw:
            # 帧首不能是 UTF-8 续字节，说明没有从字符中间切开。
            assert raw[0] & 0xC0 != 0x80
        raw.decode("utf-8")
    assert b"".join(raws) == text.encode("utf-8")
    assert "".join(frames) == text


def test_ascii_and_exact_limit():
    limit = NOISE_FRAME_PLAINTEXT_BYTES
    exact = "A" * limit
    frames = split_utf8_frames(exact)
    assert frames == [exact]
    _assert_frames(exact, frames, limit)

    over = "A" * (limit + 1)
    frames = split_utf8_frames(over)
    assert len(frames) == 2
    assert prompt_nbytes(frames[0]) == limit
    assert frames[1] == "A"
    _assert_frames(over, frames, limit)

    longer = "x" * (limit * 2 + 100)
    frames = split_utf8_frames(longer)
    assert [prompt_nbytes(frame) for frame in frames] == [limit, limit, 100]
    _assert_frames(longer, frames, limit)
    print("PASS: ASCII frames stay at the plaintext limit and reassemble")


def test_cjk_and_emoji_cross_the_boundary():
    # limit 落在「你」的第二个字节上：ab + e4 bd a0，第 4 字节是续字节。
    text = "ab" + CJK
    assert text.encode("utf-8") == b"ab\xe4\xbd\xa0"
    frames = split_utf8_frames(text, 4)
    assert frames == ["ab", CJK]
    _assert_frames(text, frames, 4)

    # 恰好装下两个汉字（6 字节），再多一个就换帧。
    packed = CJK * 2
    assert prompt_nbytes(packed) == 6
    assert split_utf8_frames(packed, 6) == [packed]
    spilled = packed + CJK
    frames = split_utf8_frames(spilled, 6)
    assert frames == [packed, CJK]
    _assert_frames(spilled, frames, 6)

    # emoji 跨过上限：前面的 ASCII 留下，4 字节字符整颗进下一帧。
    emoji_text = "ab" + EMOJI
    assert len(EMOJI.encode("utf-8")) == 4
    frames = split_utf8_frames(emoji_text, 5)
    assert frames == ["ab", EMOJI]
    _assert_frames(emoji_text, frames, 5)

    # 生产上限：59998 个 ASCII 后再放 emoji，60000 会切进这颗字符。
    head = "a" * (NOISE_FRAME_PLAINTEXT_BYTES - 2)
    crossed = head + EMOJI
    frames = split_utf8_frames(crossed)
    assert frames[0] == head
    assert EMOJI in frames[1]
    assert EMOJI not in frames[0]
    _assert_frames(crossed, frames, NOISE_FRAME_PLAINTEXT_BYTES)

    # 20000 个「你」正好 60000 字节，必须是一帧；再加一个才拆。
    exact_cjk = CJK * (NOISE_FRAME_PLAINTEXT_BYTES // 3)
    assert prompt_nbytes(exact_cjk) == NOISE_FRAME_PLAINTEXT_BYTES
    assert split_utf8_frames(exact_cjk) == [exact_cjk]
    frames = split_utf8_frames(exact_cjk + CJK)
    assert frames[0] == exact_cjk
    assert frames[1] == CJK
    _assert_frames(exact_cjk + CJK, frames, NOISE_FRAME_PLAINTEXT_BYTES)
    print("PASS: CJK and emoji frames never split a character")


def test_reassembly_and_hard_max():
    mixed = ("plain " + CJK + " " + EMOJI + "\n") * 3000
    frames = split_utf8_frames(mixed, 1000)
    _assert_frames(mixed, frames, 1000)
    assert len(frames) > 1

    # 调用方把 limit 设得比 65535 还大时，仍然不能发出超限帧。
    huge = "b" * (NOISE_MAX_FRAME_BYTES + 50)
    frames = split_utf8_frames(huge, limit=NOISE_MAX_FRAME_BYTES + 1000)
    _assert_frames(huge, frames, NOISE_MAX_FRAME_BYTES)
    assert max(prompt_nbytes(frame) for frame in frames) == NOISE_MAX_FRAME_BYTES
    print("PASS: reassembly equals original and no frame exceeds 65535")


def test_cap_is_bytes_and_413():
    os.environ["MUSE2API_MAX_PROMPT_CHARS"] = "nope"
    assert max_prompt_bytes() == 200000
    assert max_prompt_chars() == 200000
    os.environ["MUSE2API_MAX_PROMPT_CHARS"] = "150000"
    assert max_prompt_bytes() == 150000
    os.environ.pop("MUSE2API_MAX_PROMPT_CHARS", None)

    text = CJK * 3
    assert len(text) == 3
    assert prompt_nbytes(text) == 9
    assert ensure_prompt_within_cap(text, cap=9) == 9
    try:
        ensure_prompt_within_cap(text, cap=8)
        raise AssertionError("character-sized cap must not accept 9 UTF-8 bytes")
    except MusePromptTooLargeError as exc:
        assert "9 > 8" in str(exc)
        assert not isinstance(exc, type) and isinstance(exc, MuseGenerationError)

    old = app.CFG.max_prompt_chars
    app.CFG.max_prompt_chars = 8
    try:
        try:
            app._reject_oversized_prompt(text)
            raise AssertionError("expected HTTP 413")
        except app.HTTPException as exc:
            assert exc.status_code == 413
            assert "9 > 8" in str(exc.detail)
        app._reject_oversized_prompt("abcd")
    finally:
        app.CFG.max_prompt_chars = old
    print("PASS: prompt cap is bytes and oversize is HTTP 413")


def test_send_writes_frames_in_order():
    """_send 用的写入函数每次只带一帧，按顺序追加后等于原文。"""
    class Page:
        def __init__(self):
            self.sent = []

        def js(self, expr):
            marker = "})("
            start = expr.rindex(marker) + len(marker)
            args = expr[start:-1]
            if args.endswith(",true"):
                raw, append = args[:-5], True
            elif args.endswith(",false"):
                raw, append = args[:-6], False
            else:
                raise AssertionError(args[-30:])
            frame = json.loads(raw)
            self.sent.append((frame, append))
            assert prompt_nbytes(frame) <= 8
            return len("".join(item for item, _ in self.sent))

    directory = tempfile.mkdtemp(prefix="muse2api-frames-")
    try:
        engine = MuseEngine(type("Cfg", (), {"profile_dir": directory, "max_prompt_chars": 200000})())
        page = Page()
        engine.page = page
        text = "abcd" + CJK + EMOJI + "wxyz"
        frames = split_utf8_frames(text, 8)
        assert len(frames) > 1
        for index, frame in enumerate(frames):
            engine._paste_prompt_frame(frame, append=index > 0)
        assert [frame for frame, _append in page.sent] == frames
        assert page.sent[0][1] is False
        assert all(append for _frame, append in page.sent[1:])
        assert "".join(frame for frame, _append in page.sent) == text
        try:
            engine._paste_prompt_frame("z" * (NOISE_MAX_FRAME_BYTES + 1), append=False)
            raise AssertionError("oversize frame was pasted")
        except MuseGenerationError as exc:
            assert "65535" in str(exc)
    finally:
        os.rmdir(directory)
    print("PASS: frames are pasted in order and reassemble to the original")


def main():
    test_ascii_and_exact_limit()
    test_cjk_and_emoji_cross_the_boundary()
    test_reassembly_and_hard_max()
    test_cap_is_bytes_and_413()
    test_send_writes_frames_in_order()
    print("PASS: noise frame chunking")


if __name__ == "__main__":
    main()
