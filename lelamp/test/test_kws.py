"""Local wake-word test: listen for 'lao deng' and print detections."""
import argparse
import os
import subprocess
import sys

import sounddevice as sd
import sherpa_onnx


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--keywords", required=True)
    p.add_argument("--device", default=None)
    args = p.parse_args()

    root = args.model
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=f"{root}/tokens.txt",
        encoder=f"{root}/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        decoder=f"{root}/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        joiner=f"{root}/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        keywords_file=args.keywords,
        num_threads=1,
        provider="cpu",
        keywords_score=1.0,
        keywords_threshold=0.25,
    )
    stream = spotter.create_stream()
    print("Listening for: lao deng (Ctrl-C to stop)", flush=True)
    capture = subprocess.Popen(
        ["arecord", "-D", os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0"),
         "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", "48000"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        while True:
            assert capture.stdout is not None
            raw = capture.stdout.read(48000 // 10 * 2 * 2)
            if len(raw) != 48000 // 10 * 2 * 2:
                raise RuntimeError("arecord stopped unexpectedly")
            stereo = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32)
            samples = (stereo.mean(axis=1)[::3] / 32768.0).copy()
            stream.accept_waveform(16000, samples)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
            result = spotter.get_result(stream)
            if result:
                print(f"WAKE: {result}", flush=True)
                spotter.reset_stream(stream)
    finally:
        capture.terminate()
        capture.wait()


if __name__ == "__main__":
    main()
