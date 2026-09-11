from __future__ import annotations
import os
from pathlib import Path
from .config import env_float

def make_spotter(model_dir: Path, keywords_file: Path) -> sherpa_onnx.KeywordSpotter:
    import sherpa_onnx
    return sherpa_onnx.KeywordSpotter(
        tokens=str(model_dir / "tokens.txt"),
        encoder=str(model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        decoder=str(model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        joiner=str(model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        keywords_file=str(keywords_file),
        num_threads=int(os.getenv("KWS_NUM_THREADS", "1")),
        provider="cpu",
        keywords_score=env_float("KWS_SCORE", 1.5),
        keywords_threshold=env_float("KWS_THRESHOLD", 0.12),
    )
