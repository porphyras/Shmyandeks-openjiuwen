# bge-small-en-v1.5, int8 ONNX

Sentence-embedding model used by the last-resort search look (`harness/embed.py`,
`HARNESS_EMBED_FALLBACK`, on by default). Shipped in the repository and the image; nothing is
downloaded at build or run time.

- Upstream model: [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5),
  license MIT (see `LICENSE`).
- Source of the ONNX graph: the fastembed export `qdrant/bge-small-en-v1.5-onnx-q`
  (`model_optimized.onnx`, float16 weights, onnxruntime-fused Attention/SkipLayerNormalization).
- Conversion: float16 weights, value types and Cast targets set to float32, then
  `onnxruntime.quantization.quantize_dynamic(..., weight_type=QInt8)` (onnxruntime 1.31.1).
- Use: CLS token of the last hidden state, L2-normalised; queries are prefixed with
  "Represent this sentence for searching relevant passages: ". Inputs truncated to 256 tokens.

| file | bytes | sha256 |
|---|---|---|
| model_int8.onnx.gz | 21879238 | 49c08288e65b8baba32121134fd0e62dad099317685d286c3d444d41681a6707 |
| model_int8.onnx (unpacked in memory) | 33492250 | 04259da8eebcac24be59fa6f3c2c0ce7e38729259b1ed03928b7fb55517a333f |
| tokenizer.json | 711396 | d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66 |

The graph is stored gzip-compressed (`gzip.compress(data, 6, mtime=0)`): random bytes of the raw
int8 weights happen to contain sequences shaped like cloud access keys, which secret scanners
would flag; the compressed file has none, and is smaller. `harness/embed.py` checks the hashes
of both files, unpacks the graph in memory, checks its hash and hands the bytes to onnxruntime;
a mismatch, a missing file or a missing package turns the look off for the run (the search then
ends as it did without it).

Check: on bench r01, the share of supporting documents in the top 5/10/20 by embedding of title
and start: int8 37.7/45.9/59.0 %, float32 41.0/42.6/59.0 %.
