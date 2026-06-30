# AI Image Processing ("Enhance") — Design Outline

> **Status: outline.** This feature **depends on Phase 0**, the
> [compute arbiter](compute-arbiter-design.md). Enhancement is a long-running,
> large-resident-model GPU consumer; it must not be built until the arbiter
> exists, at which point it lands as "just another (bulk) client."

## 1. Premise & principles

Local, offline, neural image processing (denoise / super-resolution /
artefact-and-blur removal) as a **first-class** Photonarium feature — the hard,
high-value thing that's a dependency-hell PITA to set up elsewhere, and that
builds on the AI tooling Photonarium already has working.

- **Photonarium is not an editor.** Honoured by *never mutating the original*:
  every processed result is a **new image** (see §2).
- **Reuse, don't reinvent.** The result is catalogued via the existing
  ingestion path; GPU access via the Phase 0 arbiter; model lifecycle, OOM, and
  health via existing patterns.
- **Minimum new framework risk.** Vendor model *architectures* (one `.py` each)
  and load weights ourselves on the existing `torch`; **no** `basicsr` /
  unnecessary framework bloat.
- **Permissive licences only** (Apache-2.0 / BSD / MIT-like), verified for the
  *weights*, not just the code.
- **Neural only.** No classical resize/sharpen (trivial in GIMP etc.).

## 2. Lineage data model (derived-as-new-image)

New columns on `images` (added via the documented migration pattern —
`_SQL_MIGRATIONS` entry + a `_migrate_*` method recording itself via
`record_migration()`; see `imagedb.py:236` and `audit-16-migrations.md`):

| Column | Type | Meaning |
|--------|------|---------|
| `derived_from` | TEXT FK → images.id, **ON DELETE SET NULL** | Parent image; NULL for camera originals. |
| `processing_depth` | INTEGER DEFAULT 0 | A→B = 1, B→C = 2 …; computed `parent.processing_depth + 1`. Denormalised for badge display without recursion. |
| `processing_ops` | TEXT (JSON) | Provenance of *this* image: `[{recipe, models:[…], params}]`. |

- **SET NULL on delete:** trashing an original orphans (does not cascade-delete)
  its derivatives — they were created deliberately.
- **Naming:** `<base>__enhanced_<N>.<ext>` where `N = processing_depth`, and any
  existing `__enhanced_<M>` suffix is **stripped from the parent first** — so
  `A` → `A__enhanced_1` → `A__enhanced_2`, never `A__enhanced_1__enhanced_2`.
  `import_name` preserves the true original name for display.
- **Surfacing:** reuse the existing **stack** UI (as for duplicates) for version
  stacks — original on top, depth badge ("②"). Info panel shows "Derived from →
  [original]" and a readable `processing_ops` line ("Denoise + 2× upscale —
  SwinIR"). **Suppress derivatives from duplicate stacks** (they are intentional
  near-dupes; the version relationship owns them).

## 3. Output handling

- **Write a real file, then hand it to the existing `ImportWorker`**
  (`imagedb.py:3220`) — same path manual imports use: catalogue-dir placement,
  SHA-256 dedup, `import_name`. The 7-stage pipeline then gives it thumbnails,
  embeddings, faces, scores for free. `add_image()` (`imagedb.py:1069`) gains
  the `derived_from` / `processing_ops` params.
- **Lossless output by default** (PNG / 16-bit TIFF) — re-encoding an
  AI-cleaned image as JPEG would re-introduce the artefacts just removed.
- **RAW input → new RGB file** via `rawimage.open_image()` (cannot write back to
  RAW — state plainly in the UI).
- Optional "also export to…" checkbox for an out-of-catalogue copy.

## 4. `enhance.py` module

Mirrors `caption.py` / `nima.py`, but as an **arbiter client**:

- Registers each model's loader + `est_cost` with the compute arbiter (§Phase 0);
  all inference via `arbiter.run(model_key, fn, priority=BULK)`.
- **Tiled inference** is the core primitive (overlapping tiles, feather-blend) —
  a 4× upscale is 16× the pixels and *will* OOM otherwise. Each tile is one
  `arbiter.run` call ⇒ natural yield points for interactive work, transparent
  reload if evicted between tiles.
- **Adaptive tile sizing:** first guess from device + live free VRAM
  (`torch.cuda.mem_get_info()`); shrink-and-retry on OOM (existing pattern);
  remember last good size per device for the session.
- GPU-optional (CPU works, slowly); honours `GpuHealth` states.

## 5. The "Enhance" dialog (UX)

- **A single dialog** presenting **outcomes = real model strengths**, not
  invented categories. Options map ~1:1 to installed model capabilities
  (e.g. "Reduce noise — detail-preserving", "Increase resolution 2× / 4×",
  "Remove JPEG artefacts", "Deblur"). No fictional "Restore" combo if it's just
  two obvious options chained.
- **Dynamic:** only shows capabilities whose weights are actually downloaded
  (fits the optional/offline model story).
- **Before/after compare** on a fast centre-crop before committing the full-res
  run.
- Output defaults to "save as new version"; the export checkbox is secondary.

## 6. Execution & lifecycle

High-level contract here; the resolved lifecycle is **§11**.

- **Long op → submit-and-return** (per the Phase 0 client contract): the
  endpoint enqueues and returns immediately; **does not** hold a Waitress thread.
- **Serialised by the arbiter** (one GPU job at a time) — plus a single-worker
  FIFO enhancement queue so multiple requests queue rather than collide.
- **Feedback is two coarse toasts, no progress bar** (§11): an "Enhancing…"
  acknowledgement on submit and a completion/failure toast via the existing 2s
  `/api/events` poll (`EVENT_ENHANCE_COMPLETE` / `EVENT_ENHANCE_FAILED`).
- **No user cancellation** (§11) — but the worker still honours the
  graceful-shutdown stop event between tiles (principle #12).
- **No "GPU busy" UI** — contention is absorbed as latency by the arbiter.

## 7. Model candidates (vendored arch + downloaded weights)

| Capability | Model | Licence | Notes |
|------------|-------|---------|-------|
| SR + colour denoise + JPEG-artefact | **SwinIR** | Apache-2.0 | Recommended workhorse — one architecture, separate weights cover three outcomes. **Start here.** |
| Photo super-resolution | **Real-ESRGAN** (RRDBNet) | BSD-3 | If SwinIR SR isn't crisp enough on photos. |
| Dedicated denoise | **SCUNet** | (verify) | Optional aggressive denoiser. |

- Vendor the architecture `.py`; weights downloaded via an extended
  `download_models.py` (keeps the `HF_HUB_OFFLINE` guarantee).
- **The upstream arch files carry hidden framework imports — handled
  per-dependency** (principle #17 is *no new deps without strong justification*,
  not a blanket ban, so each is judged on its merits):
  - **SwinIR → `from timm.models.layers import DropPath, to_2tuple,
    trunc_normal_`: add `timm` as a dependency.** It clears the justification
    bar — Apache-2.0, actively maintained (HuggingFace's `pytorch-image-models`),
    and its runtime deps (`torch`, `torchvision`, `pyyaml`, `huggingface_hub`,
    `safetensors`) are *all already shipped* (torch/torchvision core; the rest
    via `transformers`), so transitive cost is ~zero. It is the de-facto layer
    library for the restoration/SR model *class* this feature grows (§8), so
    adding it once lets future arch files be vendored close to upstream instead
    of hand-de-framed N times. **Obligation: timm goes in the six-file
    dependency-sync checklist.** (Reversible — if the model roster stays small,
    the inference-time usage is trivial enough to inline later: `to_2tuple` is
    one line, `DropPath` a no-op in eval, `trunc_normal_` init-time only.)
  - **Real-ESRGAN → `from basicsr.utils.registry import ARCH_REGISTRY`:
    de-frame.** `basicsr` fails the bar (a full training framework — lmdb,
    addict, yapf, … — pulled in for a single registry decorator unused at
    inference). Delete the `@ARCH_REGISTRY.register()` decorator; no dependency.
- **Verify each weights licence** before merge (code vs weights can differ;
  e.g. NAFNet needs checking) — gate behind `download_models.py`, ship nothing
  in-repo. See `audit-01-licensing.md`.

## 8. Phasing

1. **(Phase 0 — prerequisite)** Compute arbiter landed & proven.
2. **Lineage + plumbing**: schema migration, naming, derive-as-new-image via
   ImportWorker, version-stack UI, Enhance dialog shell.
3. **First model**: SwinIR (Apache-2.0) — denoise — end-to-end thin slice:
   one image, tiled, via arbiter, ingested as a depth-1 version.
4. **Expand**: SR, artefact removal, additional models as drop-in capabilities.

## 9. Distribution, install & lifecycle

Worked through against the existing model-install machinery — these reuse
established patterns rather than inventing new ones.

### 9.1 Weights required

SwinIR is the workhorse: **one vendored architecture `.py`, multiple weight
files, each unlocking a capability**. Total realistic set ~0.3–0.6 GB — smaller
than BLIP-2 (~7 GB), so **disk is not the gating factor; compute / VRAM / time
is.** That distinction drives the wizard treatment (§9.5).

| Capability | Weight file (SwinIR) | ~Size |
|------------|----------------------|-------|
| Colour denoise | `005_colorDN_*_noise{15,25,50}.pth` | ~130 MB |
| JPEG artefact removal | `006_CAR_*_jpeg{10,20,30,40}.pth` | ~130 MB |
| Super-resolution ×2/×4 | `003_realSR_*_x4_GAN.pth` | ~130 MB |

Phase-3 thin slice requires **exactly one**: the mid-level colour-denoise weight.
Real-ESRGAN (RRDBNet, BSD-3) stays the SR fallback if SwinIR isn't crisp enough.

### 9.2 Download mechanism — the LAION/NIMA pattern, not the HF one

These are raw `.pth` on GitHub releases, **not** HuggingFace `from_pretrained`
artefacts, so they follow LAION/NIMA exactly:

- `urllib.request.urlretrieve` into `<data_dir>/.enhance/<capability>.pth` (a
  subdir — several files), with the same retry loop + skip-if-exists +
  **non-fatal** semantics.
- New `download_enhance_models()` in `download_models.py`, gated on what
  `app.py --list-models` reports.
- `--list-models` JSON gains an `enhance` block (enabled capability→URL pairs,
  driven by config). This preserves the invariant *config is the single source
  of truth for downloads* and keeps `HF_HUB_OFFLINE=1` intact (urllib bypasses
  the HF offline guard, as LAION/NIMA already do).

### 9.3 Docker

The bake is already config-driven (`make models` → `download_models.py
--data-dir docker/models` → `Dockerfile.base` copies artefacts in). Enhance
weights ride along once `download_models.py` knows about them. Additions:

- `Dockerfile.base`: `COPY docker/models/.enhance/ /defaults/.enhance/`;
  `entrypoint.sh` seeds them into the data dir on first run (as it does the
  other `/defaults/` files).

**Decided: bake by default.** `enhance_enabled` defaults to True, so `make
models` pulls the enhance weights and the standard image ships with them.
Enhance is a first-class, default-on capability (§1); the ~0.5 GB image cost is
accepted. It stays harmless on low-end because it is on-demand only and never
runs during indexing (§9.5).

### 9.4 Non-Docker upgrade — no `pip` changes

The cleanest part of the story, and a hard constraint to defend:

1. `git pull` — vendored architecture `.py` + updated `download_models.py`.
2. `python download_models.py` — idempotent; pulls only the new enhance weights.
3. Restart.

The "no `basicsr`, plain `torch`" principle means the **dependency-sync
six-file checklist is not triggered** — no new dependency. If a candidate model
needs a new framework, it fails the bar. Users can also re-run the in-app wizard
(already shells out to `download_models.py` via `wizard.py`). Order:
enable-flag → download → use.

### 9.5 Wizard / low-end — keep it, don't disable it

Existing philosophy (config.py): presets tune perf params and model sizes, but
**feature flags are user decisions, not hardware-gated**. The key difference
that settles the low-end question:

> NIMA/STT run on *every image during indexing*. Enhance only ever runs
> **on-demand**, on one image, when the user clicks "Enhance".

So leaving enhance enabled on a 2 GB ARM NAS **costs nothing until used** — it
never burdens the bulk pipeline. Therefore:

- **Keep the capability available across all tiers.** The dialog's dynamic
  list only shows capabilities whose weights are actually downloaded.
- Presets set the **default tile size** (low → small, high_desktop → large);
  the dialog **warns on expected time** when running CPU-only.
- The wizard download step gets an optional **"Include image-enhancement
  models (~0.5 GB)"** toggle — default suggested by preset (off for
  low/moderate-no-GPU, on for high_*), user-overridable. Mirrored by a
  **"Download enhancement models" button in Settings** for later. Both reuse
  the existing `wizard.py` subprocess machinery — no new download plumbing.

### 9.6 Genuine decisions still open

- None blocking. The licence gate (§9.7) is cleared; the Docker bake question is
  resolved (§9.3). The SR weight choice (§9.8) is a purely *technical* trade-off.

### 9.7 Licence verification — findings

Checked the *code* and the *weights*:

- **SwinIR** — repo is **Apache-2.0**, and the README states explicitly: *"This
  project is released under the Apache 2.0 license."* No research-only or
  non-commercial restriction. Code chain is clean: SwinIR (Apache-2.0) is based
  on Swin Transformer (MIT) and KAIR (MIT). The authors release the weights
  under the same Apache-2.0 — **redistributable**.
- **Real-ESRGAN** (RRDBNet) — **BSD-3-Clause**, © Xintao Wang. No NC/research
  restriction; commercial use permitted. **Redistributable.**
- **Training data does not encumber the weights — and this is settled project
  policy, not a judgement call.** Dataset licences restrict the *datasets*, not
  models trained on them (the basis on which CLIP, BLIP, etc. are distributed).
  Photonarium already relies on this in writing: **`LICENSES.md` ships FaceNet's
  `InceptionResnetV1` — a core, required model — trained on VGGFace2 (CC BY-NC
  4.0), distributed as MIT weights**, with a footnote stating the dataset's
  non-commercial term does not propagate to the weights. **FFHQ** (in some
  SwinIR-L weights) is the *same* situation with *weaker* constraints on us:
  optional rather than required, authors assert Apache-2.0 rather than MIT, and
  its extra share-alike clause — like the NC clause — is a *dataset* term that
  by the project's established position never reaches the weights. So FFHQ is a
  non-issue here, consistent with what already ships.
- **On merge, add SwinIR (Apache-2.0) and Real-ESRGAN (BSD-3) to the
  "Pre-trained Models & Weights" table in `LICENSES.md`.**

### 9.8 SR weight choice — purely technical

Denoise and JPEG-artefact removal use **SwinIR-M / DFWB** weights and are clear.
For super-resolution the trade-off is *not* licensing — it is:

| | **SwinIR-L SR** | **Real-ESRGAN** (RRDBNet) |
|------|-----------------|---------------------------|
| New vendored code | **None** — reuses the SwinIR arch already vendored for denoise/JPEG | A second architecture (RRDBNet `.py`) |
| Faces | **Better** (FFHQ training helps — relevant for a people-heavy library) | Weaker on faces |
| Detail fidelity | Generally sharper | Robust; can over-smooth / invent texture |
| CPU latency | Slower (large transformer) | **Lighter / faster** |

**Leaning SwinIR-L** (single architecture throughout, better on the faces a
photo catalogue is full of), with **Real-ESRGAN as the fallback if CPU latency
proves unacceptable** during the phase-3 tiling work. Confirm against real
tiled-inference numbers before locking in.

## 10. Other open questions

- Config surface: per-model enable, default tile size (advanced override),
  output format default — added to the `Config` dataclass.
- Version-stack vs duplicate-stack interaction details (suppression rules).
- Whether `processing_ops` should be queryable (e.g. "show all upscaled images")
  — likely a later filter, not v1.
- Re-enhance depth: unbounded (depth badge keeps it honest); no artificial cap.

## 11. Job lifecycle (resolved)

Deliberately minimal — a minutes-long, fire-and-forget background op needs
tracking, not scheduling (GPU contention is the arbiter's job, one layer down).

**State machine:** `queued → running → done | failed`. No `cancelled` state.

- **In-memory only.** A FIFO `queue.Queue` + a single worker thread; **no DB
  table, no migration.** The durable artefact is the resulting catalogued image;
  in-flight job state is ephemeral. An app restart mid-job simply forgets it (the
  partial output is discarded) and the user re-triggers — acceptable for an
  on-demand op.
- **Submit-and-return.** `POST /api/enhance {image_id, recipe}` enqueues and
  returns immediately (202); **never** holds a Waitress thread. **No job id is
  exposed** — with no progress polling and no cancellation, the frontend needs no
  later handle.
- **Single worker, FIFO.** One job fully processed before the next:
  load source → tile → per-tile `arbiter.run(..., BULK)` → feather-blend →
  write lossless file → hand to `ImportWorker` (catalogues it and records
  `derived_from` / `processing_ops` / `processing_depth`). The arbiter alone
  handles GPU contention with indexing; the worker does not re-solve it.
- **No progress bar.** Feedback is two coarse toasts: an immediate "Enhancing…"
  acknowledgement on submit, and a completion/failure toast via the global event
  bus. `EVENT_ENHANCE_COMPLETE` carries the new image id so *any* client can
  surface the new version in its stack; correlation is by source `image_id`, so
  no job id is needed.
- **No user cancellation.** Removes the cancel UX and the cancel stop-flag.
  **Shutdown hygiene is separate and still required** (principle #12): the worker
  checks the graceful-shutdown stop event between tiles and abandons in-flight
  work on shutdown, discarding the partial output.
- **Failure → `failed` + toast with a plain reason.** OOM at the smallest tile,
  unreadable/missing source, disk-full on write → mark failed, clean up any
  partial file, emit `EVENT_ENHANCE_FAILED`. Duplicate output is self-healing —
  `ImportWorker`'s SHA-256 dedup means a repeat run just points at the existing
  image (wasted compute, no harm); an explicit "already queued for this image"
  guard is an optional nicety, not required.
- **Template:** the existing **transcode worker** — same shape (background
  single worker, FIFO, completion event), different payload.

**New constants:** `EVENT_ENHANCE_COMPLETE`, `EVENT_ENHANCE_FAILED`
(`imagedb.py`), handled in `appstate/events.js`. **New route:**
`POST /api/enhance`.
