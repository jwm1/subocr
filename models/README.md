# English recognition model

`en_PP-OCRv4_rec.onnx` is the English PP-OCRv4 mobile recognition model distributed by RapidOCR. The model is stored once, in `subocr_assets/en_PP-OCRv4_rec.onnx`, and included in the installable wheel. It is used only for `en` regions; the Chinese detector and Chinese recognizer come with the pinned RapidOCR Python package.

- Source: https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile.onnx
- Upstream model manifest: https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml
- SHA-256: `e8770c967605983d1570cdf5352041dfb68fa0c21664f49f47b155abd3e0e318`
- Retrieved and checksum verified: 2026-10-23.
- Upstream repository license is included as [subocr_assets/LICENSE-RapidOCR](../subocr_assets/LICENSE-RapidOCR) (Apache 2.0).

Extraction is offline. An alternative compatible model can be supplied with `--english-model`; its content hash is included in the OCR cache identity.

To restore the supplied model if missing, run from the project root:

```bash
curl -L --fail --retry 2 \
  -o subocr_assets/en_PP-OCRv4_rec.onnx \
  https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile.onnx
printf '%s\n' \
  'e8770c967605983d1570cdf5352041dfb68fa0c21664f49f47b155abd3e0e318  subocr_assets/en_PP-OCRv4_rec.onnx' \
  | sha256sum --check
```
