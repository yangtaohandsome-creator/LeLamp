from __future__ import annotations
import os
from pathlib import Path
from .config import env_float

def make_spotter(
    model_dir: Path,
    keywords_file: Path,
    *,
    score: float | None = None,
    threshold: float | None = None,
) -> sherpa_onnx.KeywordSpotter:
    import sherpa_onnx
    return sherpa_onnx.KeywordSpotter(
        tokens=str(model_dir / "tokens.txt"),
        encoder=str(model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        decoder=str(model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        joiner=str(model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        keywords_file=str(keywords_file),
        num_threads=int(os.getenv("KWS_NUM_THREADS", "1")),
        provider="cpu",
        keywords_score=(env_float("KWS_SCORE", 1.5) if score is None else score),
        keywords_threshold=(
            env_float("KWS_THRESHOLD", 0.12) if threshold is None else threshold
        ),
    )


class KeywordSpotterCache:
    """One model per app; decoder streams remain owned by each voice run."""

    def __init__(self):
        self._key = None
        self._spotter = None

    def get(self, model_dir, keywords_file, factory=make_spotter):
        model_dir, keywords_file = Path(model_dir).resolve(), Path(keywords_file).resolve()
        paths = [keywords_file] + [model_dir / name for name in (
            'tokens.txt', 'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
            'decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
            'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx')]
        def signature(path):
            try:
                stat = path.stat()
                return str(path), stat.st_size, stat.st_mtime_ns
            except OSError:
                return str(path), None, None
        key = (tuple(map(signature, paths)), int(os.getenv('KWS_NUM_THREADS', '1')),
               env_float('KWS_SCORE', 1.5), env_float('KWS_THRESHOLD', .12))
        if key == self._key and self._spotter is not None:
            return self._spotter, True
        spotter = factory(model_dir, keywords_file)
        self._spotter, self._key = spotter, key
        return spotter, False

    def clear(self):
        self._spotter = self._key = None
