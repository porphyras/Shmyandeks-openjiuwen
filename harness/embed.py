"""Sentence embeddings for the last-resort search look: bge-small-en-v1.5 quantized to int8
(models/bge-small-en-v1.5-int8, see its README), run with onnxruntime and tokenizers - no torch,
no network. Loaded on first use: the files are checked against their sha256, the gzip-stored
graph is unpacked in memory and checked again. Any failure (missing package or file, wrong hash,
a runtime error) disables the look for the rest of the run."""
import gzip
import hashlib
import os
import threading

import numpy as np

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MODEL_FILES = {  # file -> sha256 (models/bge-small-en-v1.5-int8/README.md)
    "model_int8.onnx.gz": "49c08288e65b8baba32121134fd0e62dad099317685d286c3d444d41681a6707",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}
ONNX_SHA256 = "04259da8eebcac24be59fa6f3c2c0ce7e38729259b1ed03928b7fb55517a333f"  # the unpacked graph
_LOCK = threading.Lock()
_MODEL = None
_FAILED = False


def model_dir():
    return os.environ.get("HARNESS_EMBED_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "bge-small-en-v1.5-int8")


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _load():
    global _MODEL, _FAILED
    with _LOCK:
        if _MODEL is None and not _FAILED:
            try:
                d = model_dir()
                for name, digest in MODEL_FILES.items():
                    if _sha256(os.path.join(d, name)) != digest:
                        raise ValueError("checksum mismatch: %s" % name)
                import onnxruntime as ort
                from tokenizers import Tokenizer
                tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
                tok.enable_truncation(max_length=256)
                tok.enable_padding(pad_id=0, pad_token="[PAD]")
                so = ort.SessionOptions()
                so.intra_op_num_threads = int(os.environ.get("HARNESS_EMBED_THREADS", "2"))
                so.inter_op_num_threads = 1
                so.log_severity_level = 3
                with open(os.path.join(d, "model_int8.onnx.gz"), "rb") as f:
                    graph = gzip.decompress(f.read())
                if hashlib.sha256(graph).hexdigest() != ONNX_SHA256:
                    raise ValueError("checksum mismatch: unpacked graph")
                sess = ort.InferenceSession(graph, so, providers=["CPUExecutionProvider"])
                _MODEL = (tok, sess, {i.name for i in sess.get_inputs()})
            except Exception:
                _FAILED = True
        if _MODEL is None:
            raise RuntimeError("embedding model unavailable")
    return _MODEL


def embed(texts, batch=32):
    """L2-normalised CLS embeddings, one row per text."""
    tok, sess, names = _load()
    rows = []
    for k in range(0, len(texts), batch):
        enc = tok.encode_batch(list(texts[k:k + batch]))
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(ids)
        h = sess.run(None, feed)[0][:, 0]
        rows.append(h / np.maximum(np.linalg.norm(h, axis=1, keepdims=True), 1e-9))
    return np.vstack(rows) if rows else np.zeros((0, 384), dtype=np.float32)
