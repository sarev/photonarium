# Audit 05 — Works Without GPU

## Principle

> Works without GPU (CUDA/MPS optional for performance)

## Scope

- All model loading code — device selection logic
- All `torch.cuda`, `torch.mps`, `.to(device)` calls
- CUDA-specific operations (autocast, empty_cache)
- Float precision handling (float16 vs float32)
- `app/imagedb.py`, `app/faces.py`, `app/caption.py`, `app/nima.py`, `app/stt.py`

## Findings

1. **Consistent device selection pattern** — all 4 model loaders use identical CUDA → MPS → CPU fallback:
   - OpenCLIP: `imagedb.py:2502-2507`
   - MTCNN/ResNet: `faces.py:278-284`
   - BLIP/BLIP-2: `caption.py:181-186`
   - NIMA: `imagedb.py:3817-3822`

   Pattern:
   ```python
   if torch.cuda.is_available():
       device = 'cuda'
   elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
       device = 'mps'
   else:
       device = 'cpu'
   ```

2. **All tensor operations use device property**: `.to(self.device)` throughout — no hardcoded `'cuda'` in tensor movement:
   - `imagedb.py:2636,2690,2718`
   - `faces.py:465,717,743`
   - `caption.py:244`

3. **CUDA autocast guarded**: `imagedb.py:2640-2644, 2693-2697` — `torch.cuda.amp.autocast()` only when `device == 'cuda'`.

4. **Float16 conditional for CPU**: `caption.py:229,241` — uses `float16` on GPU, `float32` on CPU (CPU doesn't support float16 inference well).

5. **CUDA cache cleanup guarded**: All `torch.cuda.empty_cache()` calls preceded by `if torch.cuda.is_available()`:
   - `imagedb.py:2591,2711,2729,2778`
   - `faces.py:309,331,695,726,735,751`
   - `caption.py:255`
   - `stt.py:131`

6. **Speech-to-text auto device**: `stt.py:112-119` — faster-whisper uses `device='auto'`, `compute_type='auto'` which auto-detects and degrades gracefully.

7. **`torch.inference_mode()` and `torch.no_grad()`** used throughout — work identically on CPU/GPU:
   - `imagedb.py:2639,2692,2719,2764,2797`
   - `faces.py:468,719,744`
   - `caption.py:350`
   - `nima.py:237`

## Status

**Compliant**

## Actions

None required.
