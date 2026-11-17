from ramen_runtime import audio


def _wav() -> bytes:
    """A tenth of a second of silence, 8 kHz mono."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


def run():
    return audio(_wav(), "audio/wav")
