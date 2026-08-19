import os
import sys
# Add TripoSR folder to sys.path so we can import 'tsr' module
sys.path.append(os.path.join(os.path.dirname(__file__), "TripoSR"))

import io
import gc
from typing import Any
import asyncio
import base64
import torch
if not hasattr(torch, "float8_e8m0fnu"):
    setattr(torch, "float8_e8m0fnu", torch.float32)
import numpy as np
import cv2
from PIL import Image
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from simple_lama_inpainting import SimpleLama

try:  # package import under pytest, script import under `python backend/server.py`
    from .gpu_scheduler import run_with_gpu_lock
    from . import reconstruction_jobs
    from .reconstruction_jobs import router as reconstruction_jobs_router
except ImportError:  # pragma: no cover - exercised by the production entrypoint
    from gpu_scheduler import run_with_gpu_lock
    import reconstruction_jobs
    from reconstruction_jobs import router as reconstruction_jobs_router

# Every endpoint here is unauthenticated and several of them tie up the GPU for
# minutes, so the interactive docs are opt-in: on a machine that binds beyond
# loopback, /docs is a menu of what to call.
DOCS_ENABLED = os.environ.get("IMAGEWORLD_DOCS") == "true"

# Initialize FastAPI app
app = FastAPI(
    title="ImageWorld Local AI Inference Backend",
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url=None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "ImageWorld Local AI Inference Backend is running successfully!",
        "documentation": "http://localhost:8000/docs" if DOCS_ENABLED else None,
    }


# A full-resolution PLY is 50-60 MB and a multi-view merge more, so uploads here
# are legitimately large. But `UploadFile.read()` with no argument buffers the
# whole body in memory at once, which turns "large" into "however much the
# client feels like sending".
#
# What this cap does and does not cover, so nobody reads more into it later:
# Starlette has already parsed the multipart body before a handler runs, into a
# SpooledTemporaryFile that spills to disk past 1 MB. So the cap bounds *resident
# memory*, not what reaches the disk -- a huge upload still gets written before
# 413 comes back. Bounding that needs an ASGI-level Content-Length check or a
# reverse proxy. The budget below is also per handler, not per field, so an
# endpoint taking several files shares one allowance between them.


# 1 GiB. The largest cloud in this repo is a 194 MB splatfacto PLY and
# `/api/extract-prop` deliberately sends the full-resolution one, so the ceiling
# has to sit well above that -- a limit that rejects real work is worse than no
# limit, especially now that cross-origin callers are turned away outright.
DEFAULT_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


def _upload_limit() -> int:
    raw = os.environ.get("IMAGEWORLD_MAX_UPLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        return max(1, int(raw))
    except ValueError:
        print(f"warning: ignoring IMAGEWORLD_MAX_UPLOAD_BYTES={raw!r} (not a number)")
        return DEFAULT_MAX_UPLOAD_BYTES


MAX_UPLOAD_BYTES = _upload_limit()
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024


class UploadBudget:
    """A byte allowance shared across every upload in one request."""

    def __init__(self, limit: int = MAX_UPLOAD_BYTES) -> None:
        self.limit = limit
        self.spent = 0

    def take(self, count: int) -> None:
        self.spent += count
        if self.spent > self.limit:
            raise HTTPException(
                status_code=413,
                detail=f"Uploads exceed the {self.limit // (1024 * 1024)} MB limit for one request",
            )


async def read_upload(file: UploadFile, budget: UploadBudget | None = None) -> bytes:
    """Buffer an upload whole, rejecting anything past the budget rather than dying on it.

    Charges the budget from the declared size where the multipart parser gives
    one, so the common path is still a single `read()` and costs exactly what it
    did before. Accumulating chunks and joining them would hold the file twice
    at once, and a 194 MB point cloud alongside SAM and SHARP on a 16 GB card is
    not the place to double anything.
    """
    budget = budget or UploadBudget()
    size = getattr(file, "size", None)
    if isinstance(size, int):
        budget.take(size)
        return await file.read()

    # No declared size: pay the double to keep the cap honest.
    chunks: list[bytes] = []
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        budget.take(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
] + [
    origin.strip()
    for origin in os.environ.get("IMAGEWORLD_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    """Refuse writes from pages this app did not serve.

    CORS does not do this. It decides who may *read* a response, and the
    browser enforces that after the request has already arrived and run. A
    `multipart/form-data` POST with no custom headers is a "simple request":
    it is sent without a preflight, so any page open in the user's browser can
    start a Splatfacto job here and simply not read the reply.

    That matters because the frontend talks to this server directly from the
    browser (see `CreateWorldModal`'s reconstruction upload), so the guard on
    the Next.js routes protects none of the endpoints that actually hold the
    GPU. This is where it has to be.

    A missing `Origin` means the caller is not a browser -- curl, the Next
    server's own fetches, a test. Those are allowed: anyone who can run a
    process on this machine can already reach the files directly, so refusing
    them buys nothing and would break both the server-side routes and the
    documented CLI usage.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse(
                {"detail": "Cross-origin writes are not accepted"},
                status_code=403,
            )
    return await call_next(request)


# Enable CORS for Next.js app communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(reconstruction_jobs_router)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Using device: {DEVICE}")

# Global references for lazy loading
sam_generator = None
tsr_model = None
sfx_pipeline = None
lama_model = None

def get_lama():
    global lama_model
    if lama_model is None:
        print("Loading LaMa inpainting model...")
        lama_model = SimpleLama()
    return lama_model

# Helper to clear CUDA VRAM
def clear_vram():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


# --- Releasing VRAM ----------------------------------------------------------
#
# Every model in this file is a lazily-built module global that, once loaded, is
# never released. `clear_vram()` returns the allocator's *cached* blocks to the
# driver, but it cannot touch weights a live reference still holds, so the
# process keeps whatever it has loaded until it exits.
#
# That is harmless while all the work happens in here -- `run_heavy` already
# serialises it -- and it stops being harmless the moment something outside this
# process needs the card. `splatfacto_prototype.py` runs `ns-train` as its own
# subprocess, which cannot see this process's allocations, so a video
# reconstruction started after an image generation OOMs on a 16 GB card with the
# weights still resident here.
#
# Dropping the reference is all it takes. Nothing here carries state between
# requests: the next call lazy-loads again, paying the load time once.

MODEL_GLOBALS = (
    "sam_generator",
    "sam2_image_predictor",
    "sam3_model",
    "sam3_processor",
    "tsr_model",
    "sfx_pipeline",
    "lama_model",
    "sharp_predictor",
)


def loaded_models() -> list[str]:
    """Which model globals currently hold weights."""
    return [name for name in MODEL_GLOBALS if globals().get(name) is not None]


def unload_models() -> dict[str, Any]:
    """Drop every loaded model and hand its VRAM back to the driver.

    Safe to call at any time -- callers that need a model simply reload it. It
    is not safe to call *during* inference, so anything that runs on the GPU
    takes `GPU_JOB_LOCK` first.
    """
    released = loaded_models()
    for name in released:
        globals()[name] = None
    clear_vram()

    freed = {}
    if torch.cuda.is_available():
        free_bytes, total_bytes = torch.cuda.mem_get_info()
        freed = {
            "vram_free_bytes": int(free_bytes),
            "vram_total_bytes": int(total_bytes),
        }
    if released:
        print(f"Unloaded models: {', '.join(released)}")
    return {"released": released, **freed}



# --- Concurrency -------------------------------------------------------------
#
# Every model here is a lazily-built global on a single GPU, so two requests
# running at once would race for both the weights and the VRAM. They are also
# synchronous: calling them straight from an `async def` handler blocks the
# event loop, which starved even `GET /` for nine seconds during a SHARP run and
# made the frontend's three-second health check report the backend as offline.
#
# `run_heavy` fixes both: the work moves to a worker thread so the loop stays
# responsive, and the semaphore keeps heavy jobs strictly one at a time, so
# concurrent callers queue instead of exhausting VRAM.

HEAVY_JOB_LOCK = asyncio.Semaphore(1)


async def run_heavy(fn, *args, **kwargs):
    """Run one blocking GPU/compute job off the event loop, serialised."""
    async with HEAVY_JOB_LOCK:
        return await asyncio.to_thread(run_with_gpu_lock, fn, *args, **kwargs)


async def run_off_loop(fn, *args, **kwargs):
    """Run short blocking work off the event loop without taking the lock."""
    return await asyncio.to_thread(fn, *args, **kwargs)


@app.post("/api/unload")
async def unload_models_endpoint():
    """Release model weights so another process can use the GPU.

    Goes through `run_heavy` so it queues behind any inference in flight rather
    than pulling weights out from under it.
    """
    return await run_heavy(unload_models)


# Reconstruction spawns `ns-train` as its own process while holding the GPU
# lock; this is how it reaches back in to free the weights this process holds.
reconstruction_jobs.release_gpu_models = unload_models

# --- Segmentation backends (SAM 2 automatic + SAM 3 concept-promptable) -------

sam3_model = None
sam3_processor = None

# Default indoor prop/furniture vocabulary used to drive SAM 3 concept prompts.
# Each concept is queried independently; SAM 3 returns every matching instance,
# which gives us real semantic labels (chair, monitor, …) instead of object_N.
DEFAULT_INDOOR_CONCEPTS = [
    "chair", "armchair", "sofa", "couch", "table", "desk", "bed", "bench", "stool",
    "lamp", "monitor", "television", "computer", "keyboard", "laptop",
    "potted plant", "vase", "book", "bottle", "cup", "bowl",
    "backpack", "bag", "pillow", "cushion", "blanket",
    "cabinet", "shelf", "bookshelf", "dresser", "nightstand",
    "clock", "picture frame", "mirror", "rug", "trash can",
    "box", "speaker", "guitar", "fan", "radiator", "toy",
]

SAM3_SCORE_THRESHOLD = 0.4   # discard low-confidence concept detections
SAM3_DEDUP_IOU = 0.7         # merge instances that overlap across concepts


def get_sam3():
    """Lazy-load the SAM 3 image model + processor.

    Weights live on the gated HF repo `facebook/sam3` — request access and run
    `huggingface-cli login` first. The `sam3` package must be installed
    (`pip install -e .` from facebookresearch/sam3).
    """
    global sam3_model, sam3_processor
    if sam3_model is None:
        print("Loading SAM 3 image model (facebook/sam3)...")
        import sam3
        from sam3.model_builder import build_sam3_image_model
        from sam3.model.sam3_image_processor import Sam3Processor

        # The text encoder's BPE vocab ships inside the package assets. Resolve it
        # via __path__ (robust to editable installs / namespace shadowing where
        # sam3.__file__ can be None), with a fallback to the known clone location.
        bpe_name = os.path.join("assets", "bpe_simple_vocab_16e6.txt.gz")
        candidates = [os.path.join(base, bpe_name) for base in list(getattr(sam3, "__path__", []))]
        candidates.append(os.path.join(os.path.dirname(__file__), "sam3", "sam3", bpe_name))
        bpe_path = next((c for c in candidates if os.path.exists(c)), None)
        if bpe_path is None:
            raise RuntimeError(f"SAM 3 BPE vocab not found. Looked in: {candidates}")

        # HF_ENDPOINT (e.g. https://hf-mirror.com) is honoured automatically by
        # huggingface_hub for the gated facebook/sam3 weight download.
        sam3_model = build_sam3_image_model(bpe_path=bpe_path, device=DEVICE, load_from_HF=True)
        sam3_processor = Sam3Processor(sam3_model, device=DEVICE)
    return sam3_model, sam3_processor


def _to_numpy(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    return np.asarray(value)


def _encode_mask(mask_bool):
    """Serialize a boolean HxW mask to a base64 PNG string."""
    mask_u8 = (mask_bool.astype(np.uint8) * 255)
    _, encoded = cv2.imencode(".png", mask_u8)
    return base64.b64encode(encoded).decode("utf-8")


def _bbox_iou(a, b):
    """IoU of two [x, y, w, h] boxes."""
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _segment_sam2(img_np, h, w):
    """Automatic, label-free mask generation with SAM 2 (original behaviour)."""
    global sam_generator
    if sam_generator is None:
        print("Loading SAM 2 Automatic Mask Generator...")
        from sam2.build_sam import build_sam2
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

        # For 16GB GPU, hiera_large is ideal; fall back to base_plus if missing.
        checkpoint_path = os.path.join(os.path.dirname(__file__), "checkpoints", "sam2_hiera_large.pt")
        model_cfg = "configs/sam2/sam2_hiera_l.yaml"
        if not os.path.exists(checkpoint_path):
            checkpoint_path = os.path.join(os.path.dirname(__file__), "checkpoints", "sam2_hiera_base_plus.pt")
            model_cfg = "configs/sam2/sam2_hiera_b+.yaml"
        if not os.path.exists(checkpoint_path):
            raise HTTPException(
                status_code=500,
                detail="SAM 2 checkpoint not found. Please run download_models.py first.",
            )
        sam = build_sam2(model_cfg, checkpoint_path, device=DEVICE)
        sam_generator = SAM2AutomaticMaskGenerator(sam)

    print("Running SAM 2 mask generation...")
    masks = sam_generator.generate(img_np)

    results = []
    for i, mask_data in enumerate(masks):
        bbox = [int(x) for x in mask_data["bbox"]]  # [x, y, w, h]
        area = float(mask_data["area"])
        # Ignore tiny noise segments (less than 1% of the image)
        if area < (h * w * 0.01):
            continue
        results.append({
            "id": f"object_{i}",
            "bbox": bbox,
            "area": area,
            "predicted_iou": float(mask_data["predicted_iou"]),
            "stability_score": float(mask_data["stability_score"]),
            "mask": _encode_mask(mask_data["segmentation"].astype(bool)),
            "label": None,
        })
    return results


def _segment_sam3(pil_image, h, w, concepts):
    """Concept-promptable segmentation with SAM 3 — returns semantic labels.

    Queries each concept independently, keeps confident instances, derives the
    bbox from the mask itself (robust to box-format differences), then dedups
    instances that overlap across concepts (e.g. 'sofa' vs 'couch').
    """
    _, processor = get_sam3()

    # SAM 3 runs in bfloat16 — inference must happen inside autocast + inference_mode
    # (otherwise: "mat1 and mat2 must have the same dtype"). We convert each concept's
    # masks to numpy immediately so GPU tensors are released as we iterate.
    collected = []
    with torch.inference_mode(), torch.autocast(DEVICE, dtype=torch.bfloat16):
        state = processor.set_image(pil_image)
        for concept in concepts:
            try:
                output = processor.set_text_prompt(state=state, prompt=concept)
            except Exception as exc:
                print(f"SAM 3 concept '{concept}' failed: {exc}")
                continue

            # set_text_prompt returns the updated state; results live under these keys
            # (see sam3.visualization_utils.plot_results): masks[i] is [1, H, W].
            masks = output.get("masks")
            scores = output.get("scores")
            if masks is None:
                continue
            count = len(scores) if scores is not None else len(masks)

            for j in range(count):
                if scores is not None:
                    sc = scores[j]
                    score = float(sc.item() if hasattr(sc, "item") else sc)
                else:
                    score = 1.0
                if score < SAM3_SCORE_THRESHOLD:
                    continue
                mask_arr = np.squeeze(_to_numpy(masks[j]))  # [1,H,W] or [H,W] -> [H,W]
                if mask_arr.ndim != 2:
                    continue
                mask_bool = mask_arr > 0.5
                # Ensure the mask is at original image resolution for cropping/inpaint.
                if mask_bool.shape != (h, w):
                    mask_bool = cv2.resize(
                        mask_bool.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST
                    ).astype(bool)
                ys, xs = np.where(mask_bool)
                if xs.size == 0 or ys.size == 0:
                    continue
                x_min, x_max = int(xs.min()), int(xs.max())
                y_min, y_max = int(ys.min()), int(ys.max())
                bbox = [x_min, y_min, x_max - x_min + 1, y_max - y_min + 1]
                area = float(mask_bool.sum())
                if area < (h * w * 0.005):  # SAM 3 masks are cleaner; allow smaller props
                    continue
                collected.append({"concept": concept, "score": score, "bbox": bbox, "area": area, "mask_bool": mask_bool})

    # Keep highest-confidence instances first, drop near-duplicate overlaps.
    collected.sort(key=lambda c: c["score"], reverse=True)
    kept = []
    for cand in collected:
        if any(_bbox_iou(cand["bbox"], k["bbox"]) > SAM3_DEDUP_IOU for k in kept):
            continue
        kept.append(cand)

    results = []
    for i, c in enumerate(kept):
        results.append({
            "id": f"object_{i}",
            "bbox": c["bbox"],
            "area": c["area"],
            "predicted_iou": c["score"],
            "stability_score": c["score"],
            "mask": _encode_mask(c["mask_bool"]),
            "label": c["concept"],
        })
    return results


@app.post("/api/segment")
async def segment_image(
    file: UploadFile = File(...),
    segmenter: str = Form(None),
    concepts: str = Form(None),
):
    """
    Segment foreground objects and return image dimensions + a list of masks.

    `segmenter` selects the backend ("sam2" | "sam3"); when omitted it falls back
    to the IMAGEWORLD_SEGMENTER env var, then "sam2". SAM 3 attaches a semantic
    `label` to each object; SAM 2 returns `label: null`. If SAM 3 is requested but
    unavailable (not installed / no weights), we degrade gracefully to SAM 2 so
    the pipeline never hard-fails.
    """
    try:
        contents = await read_upload(file)
        pil_image = Image.open(io.BytesIO(contents)).convert("RGB")
        img_np = np.array(pil_image)
        h, w, _ = img_np.shape

        mode = (segmenter or os.environ.get("IMAGEWORLD_SEGMENTER") or "sam2").lower()
        concept_list = (
            [c.strip() for c in concepts.split(",") if c.strip()]
            if concepts else DEFAULT_INDOOR_CONCEPTS
        )

        def work():
            if mode != "sam3":
                return _segment_sam2(img_np, h, w), "sam2"
            try:
                return _segment_sam3(pil_image, h, w, concept_list), "sam3"
            except Exception as exc:
                print(f"SAM 3 unavailable ({exc}); falling back to SAM 2.")
                return _segment_sam2(img_np, h, w), "sam2"

        results, used = await run_heavy(work)

        print(f"[{used}] generated {len(results)} object proposals.")
        return {"width": w, "height": h, "objects": results, "segmenter_used": used}

    except HTTPException:
        # A deliberate 4xx (an oversized upload, an unusable selection) must not
        # be flattened into a 500 by the catch-all below.
        raise
    except Exception as e:
        print(f"Segmentation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()


sam2_image_predictor = None


def get_sam2_predictor():
    """Lazy-load SAM 2's interactive image predictor (for click/point prompts)."""
    global sam2_image_predictor
    if sam2_image_predictor is None:
        print("Loading SAM 2 image predictor (point prompts)...")
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        checkpoint_path = os.path.join(os.path.dirname(__file__), "checkpoints", "sam2_hiera_large.pt")
        model_cfg = "configs/sam2/sam2_hiera_l.yaml"
        if not os.path.exists(checkpoint_path):
            checkpoint_path = os.path.join(os.path.dirname(__file__), "checkpoints", "sam2_hiera_base_plus.pt")
            model_cfg = "configs/sam2/sam2_hiera_b+.yaml"
        if not os.path.exists(checkpoint_path):
            raise HTTPException(status_code=500, detail="SAM 2 checkpoint not found. Run download_models.py first.")
        sam2_model = build_sam2(model_cfg, checkpoint_path, device=DEVICE)
        sam2_image_predictor = SAM2ImagePredictor(sam2_model)
    return sam2_image_predictor


@app.post("/api/segment-point")
async def segment_point(
    file: UploadFile = File(...),
    x: float = Form(...),
    y: float = Form(...),
):
    """
    Interactive segmentation: given a click at (x, y) in original-image pixels,
    return the single best object mask there (base64 PNG) + its bbox. Lets the
    user hand-pick exactly which objects become 3D props.
    """
    try:
        contents = await read_upload(file)
        pil_image = Image.open(io.BytesIO(contents)).convert("RGB")
        img_np = np.array(pil_image)
        h, w, _ = img_np.shape

        def work():
            predictor = get_sam2_predictor()
            predictor.set_image(img_np)
            return predictor.predict(
                point_coords=np.array([[float(x), float(y)]]),
                point_labels=np.array([1]),
                multimask_output=True,
            )

        masks, scores, _ = await run_heavy(work)
        best = int(np.argmax(scores))
        mask_bool = masks[best].astype(bool)

        ys, xs = np.where(mask_bool)
        if xs.size == 0 or ys.size == 0:
            raise HTTPException(status_code=400, detail="No object found at that point.")
        x_min, x_max = int(xs.min()), int(xs.max())
        y_min, y_max = int(ys.min()), int(ys.max())
        bbox = [x_min, y_min, x_max - x_min + 1, y_max - y_min + 1]

        return {
            "width": w,
            "height": h,
            "bbox": bbox,
            "area": float(mask_bool.sum()),
            "score": float(scores[best]),
            "mask": _encode_mask(mask_bool),
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Point segmentation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()

@app.post("/api/inpaint")
async def inpaint_image(
    image: UploadFile = File(...),
    masks: list[UploadFile] = File(...),
):
    """
    Remove segmented foreground objects and fill background using LaMa.
    Returns the cleaned background image (Clean Plate).

    All masks are erased in a single pass over their union. Erasing them one at
    a time instead re-ran LaMa on its own output, so each pass hallucinated on
    top of the previous hallucination.
    """
    try:
        # `masks` is an unbounded list, so the allowance has to be shared --
        # a per-file cap here would be no cap at all.
        budget = UploadBudget()
        img_bytes = await read_upload(image, budget)
        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        union = None
        for mask_file in masks:
            mask_np = np.array(
                Image.open(io.BytesIO(await read_upload(mask_file, budget))).convert("L").resize(pil_img.size, Image.NEAREST)
            )
            union = mask_np if union is None else np.maximum(union, mask_np)
        if union is None:
            raise HTTPException(status_code=400, detail="No masks provided")

        pil_mask = Image.fromarray((union > 127).astype(np.uint8) * 255, mode="L")

        print(f"Running LaMa inpainting over the union of {len(masks)} mask(s)...")

        def work():
            result = get_lama()(pil_img, pil_mask)
            # LaMa pads the input up to a multiple of 8 and returns the padded
            # canvas. Downstream every mask, the splat's intrinsics and the
            # object placements are expressed in source-image pixels, so hand
            # back exactly the size we were given.
            if result.size != pil_img.size:
                result = result.crop((0, 0, pil_img.size[0], pil_img.size[1]))
            return result

        result_img = await run_heavy(work)

        img_io = io.BytesIO()
        result_img.save(img_io, "PNG")
        img_io.seek(0)
        return StreamingResponse(img_io, media_type="image/png")

    except HTTPException:
        raise
    except Exception as e:
        print(f"Inpainting failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()

@app.post("/api/crop")
async def crop_image(
    image: UploadFile = File(...),
    mask: UploadFile = File(...)
):
    """
    Crop the foreground object out of the original image using the SAM 2 mask.
    Returns a transparent PNG cropped tightly to the object bounding box.
    """
    try:
        budget = UploadBudget()
        img_bytes = await read_upload(image, budget)
        mask_bytes = await read_upload(mask, budget)
        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        pil_mask = Image.open(io.BytesIO(mask_bytes)).convert("L")

        # Get bounding box of mask (where mask > 0)
        mask_np = np.array(pil_mask)
        pos = np.where(mask_np > 0)
        if len(pos[0]) == 0 or len(pos[1]) == 0:
            raise HTTPException(status_code=400, detail="Empty mask provided")

        y_min, y_max = int(np.min(pos[0])), int(np.max(pos[0]))
        x_min, x_max = int(np.min(pos[1])), int(np.max(pos[1]))

        # Cheap enough that it need not queue behind a generation, but still
        # worth keeping off the event loop.
        def work():
            rgba_img = pil_img.copy()
            rgba_img.putalpha(pil_mask)
            cropped = rgba_img.crop((x_min, y_min, x_max + 1, y_max + 1))
            buf = io.BytesIO()
            cropped.save(buf, "PNG")
            buf.seek(0)
            return buf

        return StreamingResponse(await run_off_loop(work), media_type="image/png")

    except HTTPException:
        # A deliberate 4xx (an oversized upload, an unusable selection) must not
        # be flattened into a 500 by the catch-all below.
        raise
    except Exception as e:
        print(f"Cropping failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/image-to-3d")
async def image_to_3d(file: UploadFile = File(...)):
    """
    Convert a segmented/cropped object image into a 3D model (.glb) using TripoSR.
    """
    global tsr_model
    try:
        contents = await read_upload(file)
        pil_image = Image.open(io.BytesIO(contents)).convert("RGBA")

        def work():
            global tsr_model
            # Lazy load TripoSR to save VRAM
            if tsr_model is None:
                print("Loading TripoSR model...")
                from tsr.system import TSR

                # stabilityai/TripoSR downloads automatically from Hugging Face Hub
                tsr_model = TSR.from_pretrained(
                    "stabilityai/TripoSR",
                    config_name="config.yaml",
                    weight_name="model.ckpt"
                )
                tsr_model.to(DEVICE)

            print("Running TripoSR 3D generation...")
            from tsr.utils import remove_background, resize_foreground

            # Run background removal (if not already cropped) and resize object
            # TripoSR works best with resized foreground
            processed_image = remove_background(pil_image, threshold=0.85)
            processed_image = resize_foreground(processed_image, ratio=0.85)

            # Convert RGBA to RGB by compositing over gray (TripoSR convention)
            img_np = np.array(processed_image).astype(np.float32) / 255.0
            if img_np.shape[-1] == 4:
                img_np = img_np[:, :, :3] * img_np[:, :, 3:4] + (1.0 - img_np[:, :, 3:4]) * 0.5
            processed_image = Image.fromarray((img_np * 255.0).astype(np.uint8))

            with torch.no_grad():
                scene_codes = tsr_model([processed_image], device=DEVICE)

            print("Converting scene codes to 3D mesh...")
            mesh = tsr_model.extract_mesh(scene_codes, True, resolution=256)[0]

            # TripoSR emits X-up meshes (verified by matching mesh silhouettes
            # back to the source crops: the front view is the Y/X plane at IoU
            # 0.62-0.95, against 0.12-0.52 for every other axis pair). glTF and
            # three.js are Y-up, so without this every prop lies on its side --
            # and the viewer derives an object's footprint from its bounding
            # box, so a toppled mesh also sinks into the floor. Rotating here
            # keeps the fix in one place instead of making every consumer
            # compensate.
            # 90 deg about Z: (1,0,0) -> (0,1,0), so the mesh's up axis becomes Y.
            mesh.apply_transform(np.array([
                [0.0, -1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]))

            buf = io.BytesIO()
            mesh.export(buf, file_type="glb")
            buf.seek(0)
            return mesh, buf

        mesh, glb_io = await run_heavy(work)

        # TripoSR normalises every object into roughly a unit cube, so the raw
        # mesh says nothing about real size. The caller pairs these extents with
        # the metric size measured from the splat to derive a placement scale.
        extents = mesh.bounds[1] - mesh.bounds[0]
        print(f"TripoSR generation complete (mesh extents {extents.round(3).tolist()}).")
        return Response(
            content=glb_io.read(),
            media_type="model/gltf-binary",
            headers={"X-Mesh-Extents": ",".join(f"{float(v):.6f}" for v in extents)},
        )

    except HTTPException:
        # A deliberate 4xx (an oversized upload, an unusable selection) must not
        # be flattened into a 500 by the catch-all below.
        raise
    except Exception as e:
        print(f"3D generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()

@app.post("/api/generate-sfx")
async def generate_sfx(prompt: str = Form(...), duration: float = Form(3.0)):
    """
    Generate interactive Foley/collision sound effects using AudioLDM.
    """
    global sfx_pipeline
    try:
        def work():
            global sfx_pipeline
            # Lazy load AudioLDM to save VRAM
            if sfx_pipeline is None:
                print("Loading AudioLDM pipeline...")
                from diffusers import AudioLDMPipeline

                sfx_pipeline = AudioLDMPipeline.from_pretrained(
                    "cvssp/audioldm-s-full-v2",
                    torch_dtype=torch.float16
                )
                sfx_pipeline.to(DEVICE)

            print(f"Generating SFX for prompt: '{prompt}'...")
            generator = torch.manual_seed(42)
            audio = sfx_pipeline(
                prompt,
                num_inference_steps=50,
                audio_length_in_s=duration,
                generator=generator
            ).audios[0]

            audio_np = audio.cpu().numpy() if hasattr(audio, "cpu") else audio

            import scipy.io.wavfile as wavfile
            buf = io.BytesIO()
            # AudioLDM outputs at 16kHz sampling rate
            wavfile.write(buf, 16000, audio_np)
            buf.seek(0)
            return buf

        wav_io = await run_heavy(work)

        print("SFX generation complete.")
        return Response(content=wav_io.read(), media_type="audio/wav")

    except Exception as e:
        print(f"SFX generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()

# --- Background splat generation (Apple SHARP) --------------------------------

sharp_predictor = None
SHARP_MODEL_URL = "https://ml-site.cdn-apple.com/models/sharp/sharp_2572gikvuh.pt"


def get_sharp():
    """Lazy-load the Apple SHARP single-image 3DGS predictor.

    Weights auto-download from Apple's CDN (cached by torch.hub). We import only
    `sharp.models` + `sharp.utils` so we never touch `sharp.cli`/`gsplat` (which
    is render-only and hard to build on Windows).
    """
    global sharp_predictor
    if sharp_predictor is None:
        print("Loading Apple SHARP predictor (downloads weights on first run)...")
        from sharp.models import PredictorParams, create_predictor
        state_dict = torch.hub.load_state_dict_from_url(SHARP_MODEL_URL, progress=True)
        sharp_predictor = create_predictor(PredictorParams())
        sharp_predictor.load_state_dict(state_dict)
        sharp_predictor.eval()
        sharp_predictor.to(DEVICE)
    return sharp_predictor


@torch.no_grad()
def _predict_gaussians(predictor, image, f_px, device):
    """Predict a 3D Gaussian scene from an image.

    Reimplemented from sharp.cli.predict.predict_image so we avoid importing
    sharp.cli (which eagerly imports the gsplat-backed renderer).
    """
    import torch.nn.functional as F
    from sharp.utils.gaussians import unproject_gaussians

    internal_shape = (1536, 1536)
    image_pt = torch.from_numpy(image.copy()).float().to(device).permute(2, 0, 1) / 255.0
    _, height, width = image_pt.shape
    disparity_factor = torch.tensor([f_px / width]).float().to(device)
    image_resized_pt = F.interpolate(
        image_pt[None], size=(internal_shape[1], internal_shape[0]), mode="bilinear", align_corners=True
    )
    gaussians_ndc = predictor(image_resized_pt, disparity_factor)
    intrinsics = torch.tensor(
        [[f_px, 0, width / 2, 0], [0, f_px, height / 2, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    ).float().to(device)
    intrinsics_resized = intrinsics.clone()
    intrinsics_resized[0] *= internal_shape[0] / width
    intrinsics_resized[1] *= internal_shape[1] / height
    gaussians = unproject_gaussians(
        gaussians_ndc, torch.eye(4).to(device), intrinsics_resized, internal_shape
    )
    return gaussians


@app.post("/api/image-to-splat")
async def image_to_splat(file: UploadFile = File(...)):
    """
    Generate a 3D Gaussian splat (.ply) background from a single image using SHARP.
    Intended to run on the inpainted clean plate (foreground objects removed).
    """
    import tempfile
    from pathlib import Path
    from sharp.utils import io as sharp_io
    from sharp.utils.gaussians import save_ply
    try:
        contents = await read_upload(file)

        def work():
            predictor = get_sharp()
            with tempfile.TemporaryDirectory() as td:
                in_path = Path(td) / "input.png"
                Image.open(io.BytesIO(contents)).convert("RGB").save(in_path)

                image_np, _, f_px = sharp_io.load_rgb(in_path)
                height, width = image_np.shape[:2]
                print(f"Running SHARP on {width}x{height} (f_px={f_px:.1f})...")
                gaussians = _predict_gaussians(predictor, image_np, f_px, torch.device(DEVICE))

                out_path = Path(td) / "world.ply"
                save_ply(gaussians, f_px, (height, width), out_path)
                return out_path.read_bytes()

        ply_bytes = await run_heavy(work)

        print(f"SHARP splat generated ({len(ply_bytes)} bytes).")
        return Response(content=ply_bytes, media_type="application/octet-stream")

    except HTTPException:
        # A deliberate 4xx (an oversized upload, an unusable selection) must not
        # be flattened into a 500 by the catch-all below.
        raise
    except Exception as e:
        print(f"SHARP splat generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()

def _read_splat_ply_with_camera(data: bytes):
    """Read gaussian centres, opacity and the camera SHARP reconstructed with.

    SHARP writes its intrinsics and the source resolution into the PLY as extra
    elements, which is what lets us project gaussians back onto image pixels.
    """
    ply_scalar = {
        "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
        "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
        "short": "<i2", "int16": "<i2", "ushort": "<u2", "uint16": "<u2",
        "int": "<i4", "int32": "<i4", "uint": "<u4", "uint32": "<u4",
    }

    end = data.find(b"end_header")
    if end < 0:
        raise ValueError("not a PLY file (no end_header)")
    end = data.find(b"\n", end) + 1
    header = data[:end].decode("ascii", errors="replace")
    if "format binary_little_endian" not in header:
        raise ValueError("only binary_little_endian PLY is supported")

    elements = []
    for line in (l.strip() for l in header.splitlines()):
        if line.startswith("element"):
            _, name, count = line.split()
            elements.append((name, int(count), []))
        elif line.startswith("property") and elements:
            parts = line.split()
            if parts[1] == "list":
                raise ValueError("list properties are not supported")
            elements[-1][2].append((parts[2], ply_scalar[parts[1]]))

    parsed = {}
    offset = end
    for name, count, props in elements:
        dtype = np.dtype([(p, d) for p, d in props])
        parsed[name] = np.frombuffer(data, dtype=dtype, count=count, offset=offset)
        offset += count * dtype.itemsize

    vertex = parsed.get("vertex")
    if vertex is None:
        raise ValueError("PLY has no vertex element")
    xyz = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=1).astype(np.float64)
    opacity = vertex["opacity"].astype(np.float64) if "opacity" in vertex.dtype.names else None

    if "intrinsic" not in parsed or "image_size" not in parsed:
        raise ValueError("PLY has no camera intrinsics; was it produced by SHARP?")
    K = np.asarray(parsed["intrinsic"]["intrinsic"], dtype=np.float64).reshape(3, 3)
    width, height = (int(v) for v in parsed["image_size"]["image_size"])
    return xyz, opacity, K, width, height


PLACEMENT_MAX_DISTANCE = 15.0     # ignore anything seen through a window
PLACEMENT_OPACITY_THRESHOLD = 0.35
PLACEMENT_CONTACT_BAND = 0.12     # fraction of mask height treated as its base
PLACEMENT_MIN_CONTACT_POINTS = 12


def locate_objects(ply_bytes: bytes, mask_images: list):
    """Locate each masked object in 3D using the splat's own camera.

    Objects used to be lined up on an arbitrary row in front of the camera,
    which threw away the one thing the source photo actually tells us: where
    each object sits. SHARP embeds the camera it reconstructed with, so a
    gaussian can be projected back to the pixel it came from. Reading the
    gaussians under a mask's base therefore recovers the surface the object was
    resting on, and the mask's pixel extent at that depth gives its metric size.

    `mask_images` are PIL images in any resolution; they are resampled to the
    splat's source resolution. Returns (placements, ground_plane_offset), where
    each position is viewer-space but has no ground offset applied.
    """
    xyz, opacity, K, width, height = _read_splat_ply_with_camera(ply_bytes)
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]

    depth = xyz[:, 2]
    usable = depth > 1e-6
    u = np.full(len(xyz), -1.0)
    v = np.full(len(xyz), -1.0)
    u[usable] = fx * xyz[usable, 0] / depth[usable] + cx
    v[usable] = fy * xyz[usable, 1] / depth[usable] + cy

    usable &= (u >= 0) & (u < width) & (v >= 0) & (v < height) & (depth <= PLACEMENT_MAX_DISTANCE)
    if opacity is not None:
        usable &= (1.0 / (1.0 + np.exp(-opacity))) >= PLACEMENT_OPACITY_THRESHOLD
    if usable.sum() < 1000:
        raise ValueError("too few usable gaussians to locate objects")

    ui = np.clip(u.astype(np.int32), 0, width - 1)
    vi = np.clip(v.astype(np.int32), 0, height - 1)
    idx = np.flatnonzero(usable)

    # The floor is the dominant flat horizontal band low in the scene. In viewer
    # space (SHARP's +Y points down) that is the strongest peak in the lower
    # half of the height histogram.
    viewer_y = -xyz[idx, 1]
    histogram, edges = np.histogram(viewer_y, bins=80)
    lower_half = histogram[: len(histogram) // 2]
    floor_y = float(edges[int(np.argmax(lower_half))]) if lower_half.max() > 0 else float(viewer_y.min())
    ground_plane_offset = -floor_y

    results = []
    for mask_img in mask_images:
        if mask_img.size != (width, height):
            mask_img = mask_img.resize((width, height), Image.NEAREST)
        mask_bool = np.array(mask_img.convert("L")) > 127

        ys, xs = np.where(mask_bool)
        if xs.size == 0:
            results.append({"located": False, "reason": "empty mask"})
            continue

        y_min, y_max = int(ys.min()), int(ys.max())
        x_min, x_max = int(xs.min()), int(xs.max())

        # The object's base: the bottom slice of its mask. On the clean plate
        # the object is already erased there, so those gaussians describe the
        # surface it was resting on -- the floor, or the desk it sat on.
        band_top = y_max - max(3, int((y_max - y_min) * PLACEMENT_CONTACT_BAND))
        contact = mask_bool.copy()
        contact[:band_top] = False

        hit = idx[contact[vi[idx], ui[idx]]]
        if hit.size < PLACEMENT_MIN_CONTACT_POINTS:
            # A thin or occluded base still gives a usable depth from the whole
            # mask even when its contact strip alone does not.
            hit = idx[mask_bool[vi[idx], ui[idx]]]
        if hit.size < PLACEMENT_MIN_CONTACT_POINTS:
            results.append({"located": False, "reason": "no gaussians under mask"})
            continue

        base = np.median(xyz[hit], axis=0)
        base_depth = float(base[2])

        # Pinhole: an object spanning n pixels at distance d covers
        # n * d / f metres on the plane through its base.
        width_m = float((x_max - x_min + 1) * base_depth / fx)
        height_m = float((y_max - y_min + 1) * base_depth / fy)

        results.append({
            "located": True,
            # Viewer space, rotated 180 deg about X (the manifest's flip_y), but
            # WITHOUT the ground offset applied -- the caller adds whichever
            # offset it settles on so the floor is defined in exactly one place.
            "position": [float(base[0]), float(-base[1]), float(-base[2])],
            # Depth is unobservable from one view; assume it matches width.
            "size": [width_m, height_m, width_m],
            "depth": base_depth,
            "contact_points": int(hit.size),
        })

    return results, ground_plane_offset


@app.post("/api/place-objects")
async def place_objects(
    file: UploadFile = File(...),
    masks: str = Form(...),
):
    """
    Locate segmented objects in 3D against the background splat.

    `masks` is a JSON array of base64 PNG masks in source-image resolution.
    Returns, per mask, the viewer-space `position` (the object's footprint,
    with the floor already at y=0) and its measured `size` in metres. Entries
    whose contact patch has too few gaussians report `located: false` so the
    caller can fall back rather than place them somewhere wrong.
    """
    import json

    try:
        mask_entries = json.loads(masks)
        if not isinstance(mask_entries, list):
            raise ValueError("masks must be a JSON array")

        mask_images = [
            Image.open(io.BytesIO(base64.b64decode(entry.get("mask") if isinstance(entry, dict) else entry)))
            for entry in mask_entries
        ]
        # Projecting a million gaussians is CPU-bound but still blocks for
        # seconds, so it goes through the same queue as the GPU work.
        ply_bytes = await read_upload(file)
        results, ground_plane_offset = await run_heavy(locate_objects, ply_bytes, mask_images)

        located = sum(1 for r in results if r.get("located"))
        print(f"Placed {located}/{len(results)} objects (ground offset {ground_plane_offset:.3f}).")
        return {"placements": results, "ground_plane_offset": ground_plane_offset}

    except HTTPException:
        # A deliberate 4xx (an oversized upload, an unusable selection) must not
        # be flattened into a 500 by the catch-all below.
        raise
    except Exception as e:
        print(f"Object placement failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/extract-prop")
async def extract_prop(
    file: UploadFile = File(...),
    image: UploadFile | None = File(None),
    existing: UploadFile | None = File(None),
    prop_id: str = Form("prop-0"),
    point: str | None = Form(None),
    box: str | None = Form(None),
    voxel_size: float = Form(0.03),
    metric_scale: float = Form(1.0),
):
    """Cut one movable prop out of a world's gaussians.

    Two ways to say which prop, because neither alone is good enough:

    - `point`: a splat-space [x, y, z] the user clicked. It is projected back
      through SHARP's own camera to a source pixel, SAM 2 segments the object
      there, and the mask selects the gaussians. Accurate boundaries, but SAM
      can pick the wrong thing.
    - `box`: a splat-space [[x,y,z],[x,y,z]] the user dragged. Exactly what
      they asked for, and the fallback when SAM gets it wrong.

    Connected components cannot do this job: measured across three worlds a
    single component holds 90-98% of the room mesh, because anything standing
    on the floor is fused to it at voxel resolution.

    Returns the prop's collision shell as a GLB in splat space -- the same
    space as the world collider, so the viewer applies the one transform it
    already has.
    """
    import json
    import tempfile
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    try:
        from build_props import (
            gaussians_in_box,
            gaussians_under_mask,
            merge_props,
            prop_collider,
            prop_descriptor,
            project_to_pixels,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"prop dependencies missing ({e}); pip install scikit-image trimesh",
        )

    if (point is None) == (box is None):
        raise HTTPException(status_code=400, detail="give exactly one of `point` or `box`")

    try:
        # One allowance across the cloud, the source photo and the existing
        # props file, since all three arrive on the same request.
        budget = UploadBudget()
        contents = await read_upload(file, budget)
        xyz, opacity, K, width, height = _read_splat_ply_with_camera(contents)

        if box is not None:
            corners = np.asarray(json.loads(box), dtype=np.float64)
            if corners.shape != (2, 3):
                raise HTTPException(status_code=400, detail="`box` must be [[x,y,z],[x,y,z]]")
            selected = gaussians_in_box(
                xyz, corners.min(axis=0), corners.max(axis=0), opacity=opacity
            )
            label = "box selection"
        else:
            if image is None:
                raise HTTPException(status_code=400, detail="`point` selection needs the source image")
            target = np.asarray(json.loads(point), dtype=np.float64).reshape(1, 3)
            u, v, valid = project_to_pixels(target, K, width, height)
            if not valid[0]:
                raise HTTPException(status_code=400, detail="that point is not visible to the world's camera")

            source = np.array(Image.open(io.BytesIO(await read_upload(image, budget))).convert("RGB"))
            # The photo the world was built from may have been resized since;
            # SAM works in its pixels, the projection in the splat's.
            scale_x = source.shape[1] / width
            scale_y = source.shape[0] / height

            def segment():
                predictor = get_sam2_predictor()
                predictor.set_image(source)
                return predictor.predict(
                    point_coords=np.array([[float(u[0]) * scale_x, float(v[0]) * scale_y]]),
                    point_labels=np.array([1]),
                    multimask_output=True,
                )

            masks, scores, _ = await run_heavy(segment)
            mask_bool = masks[int(np.argmax(scores))].astype(bool)
            if mask_bool.shape != (height, width):
                mask_bool = np.array(
                    Image.fromarray(mask_bool).resize((width, height), Image.NEAREST)
                )
            selected = gaussians_under_mask(xyz, K, width, height, mask_bool, opacity=opacity)
            label = f"SAM 2 mask (score {float(scores[int(np.argmax(scores))]):.2f})"

        if len(selected) == 0:
            raise HTTPException(status_code=422, detail="nothing selected there")

        previous = await read_upload(existing, budget) if existing is not None else None

        # `voxel_size` stays in the cloud's own units. Reinterpreting it as
        # metres would change the shell every existing prop was cut with, and
        # the only quantity here that has to be metric is the mass.
        scale = metric_scale if metric_scale > 0 else 1.0

        def build():
            mesh = prop_collider(xyz[selected], voxel_size=voxel_size)
            # Return the whole props GLB, not just this prop: the caller is a
            # Next.js route that only moves bytes around and has no GLB parser.
            return mesh, merge_props(previous, mesh, prop_id)

        try:
            mesh, glb_bytes = await run_off_loop(build)
        except ValueError as e:
            # Too few gaussians or too small a shell: a user problem, not a bug.
            raise HTTPException(status_code=422, detail=str(e))

        descriptor = prop_descriptor(mesh, metric_scale=scale)
        print(f"Prop '{prop_id}' extracted from {label}: {len(selected):,} gaussians, {descriptor['mass']} kg")
        return {
            "props_glb": base64.b64encode(glb_bytes).decode("ascii"),
            "prop_id": prop_id,
            "gaussian_count": int(len(selected)),
            "source": label,
            **descriptor,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Prop extraction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        clear_vram()


@app.post("/api/walkable")
async def walkable_map(
    file: UploadFile = File(...),
    flip_y: bool = Form(True),
    rotation_x: float | None = Form(None),
    metric_scale: float = Form(1.0),
    spawn_x: float | None = Form(None),
    spawn_z: float | None = Form(None),
):
    """Where a person could actually stand, as a mask over the floor plan.

    Takes the collision mesh rather than the point cloud, for two reasons. It is
    a hundred times smaller and already carries the surface split, so there is
    nothing to voxelise again. And it can be called *after* the collider, which
    is the only order that works for a video reconstruction: that world's metric
    scale is calculated from the collider's own floor height, so the scale this
    map has to be measured at does not exist until the collider is finished.

    `spawn_x` / `spawn_z` are viewer-space metres. Without them the largest
    connected region stands in for "the room", which is the right guess when the
    caller has not written scene.json yet.
    """
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    try:
        import trimesh

        from build_walkable import build_walkable_map, collider_groups
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"walkable dependencies missing ({e})")

    contents = await read_upload(file)
    scale = metric_scale if metric_scale > 0 else 1.0

    def work():
        import io as _io

        scene = trimesh.load(_io.BytesIO(contents), file_type="glb", process=False)
        if not isinstance(scene, trimesh.Scene):
            scene = trimesh.Scene(scene)
        mesh, groups = collider_groups(scene)
        if not groups:
            raise ValueError(
                "this collider predates the surface split, so there is no floor to map"
            )

        # Same reasoning as the collider: standing is decided by which way a
        # face points, so measure in the space the viewer will show.
        if rotation_x is None:
            oriented, oriented_flip_y = mesh, flip_y
        else:
            oriented = mesh.copy()
            oriented.apply_transform(
                trimesh.transformations.rotation_matrix(rotation_x, [1, 0, 0])
            )
            oriented_flip_y = False

        # `CELL_SIZE` and `CHARACTER_HEIGHT` are metres; the mesh is not.
        if scale != 1.0:
            oriented = oriented.copy()
            oriented.apply_scale(scale)

        spawn = None if spawn_x is None or spawn_z is None else (spawn_x, spawn_z)
        return build_walkable_map(
            oriented,
            groups,
            flip_y=oriented_flip_y,
            spawn_xz=spawn,
            metric_scale_factor=scale,
        )

    try:
        # Surface sampling and a flood fill: no GPU, but seconds of CPU.
        png, meta = await run_off_loop(work)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    stranded = meta["standable_area"] - meta["walkable_area"]
    print(
        f"Walkable: {meta['walkable_area']:.1f} m2 reachable of "
        f"{meta['standable_area']:.1f} m2 standable ({stranded:.1f} m2 stranded)"
    )
    return {"mask_png": base64.b64encode(png).decode("ascii"), **meta}


@app.post("/api/splat-to-collider")
async def splat_to_collider(
    file: UploadFile = File(...),
    voxel_size: float = Form(0.05),
    target_faces: int = Form(60000),
    max_distance: float = Form(15.0),
    flip_y: bool = Form(True),
    rotation_x: float | None = Form(None),
):
    """
    Derive a collision mesh + ground calibration from a Gaussian-splat PLY.

    SHARP produces Gaussians the viewer can render but not collide with, so
    without this the character controller only has the flat ground plane and a
    USD export carries no room geometry. Feed it the 500k LOD -- a 0.05 m voxel
    grid does not benefit from full resolution.

    Returns the GLB (base64) plus the `ground_plane_offset` that puts the floor
    on y=0.
    """
    import tempfile
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    try:
        from build_collider import (
            build_collider,
            classify_surfaces,
            detect_floor_height,
            labeled_collider_scene,
            read_splat_ply,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"collider dependencies missing ({e}); pip install scikit-image fast-simplification",
        )

    try:
        contents = await read_upload(file)

        # Voxelising and marching-cubing a large cloud runs for tens of seconds.
        # It needs no GPU, but it is more than heavy enough to stall the loop.
        def work():
            with tempfile.TemporaryDirectory() as td:
                ply_path = Path(td) / "world.ply"
                ply_path.write_bytes(contents)

                xyz, opacity = read_splat_ply(ply_path)
                built = build_collider(
                    xyz,
                    opacity,
                    voxel_size=voxel_size,
                    target_faces=target_faces,
                    max_distance=max_distance,
                    verbose=True,
                )
                # Both the floor height and the surface split are decided by
                # which way a face points, so they have to be measured in the
                # space the viewer will see. A rigid rotation leaves face
                # indices alone, so the groups still address `built`, which is
                # what the GLB stores -- the viewer applies the rotation itself.
                if rotation_x is None:
                    viewer_mesh = None
                    oriented, oriented_flip_y = built, flip_y
                else:
                    import trimesh

                    viewer_mesh = built.copy()
                    viewer_mesh.apply_transform(
                        trimesh.transformations.rotation_matrix(rotation_x, [1, 0, 0])
                    )
                    oriented, oriented_flip_y = viewer_mesh, False

                floor_y = detect_floor_height(oriented, flip_y=oriented_flip_y)
                ground_offset = None if floor_y is None else -floor_y
                groups = classify_surfaces(oriented, floor_y=floor_y, flip_y=oriented_flip_y)

                glb_path = Path(td) / "collider.glb"
                labeled_collider_scene(built, groups).export(glb_path)
                return built, viewer_mesh, ground_offset, groups, glb_path.read_bytes()

        mesh, viewer_mesh, ground_offset, groups, glb_bytes = await run_heavy(work)
        size = mesh.bounds[1] - mesh.bounds[0]

        # Report the extent in viewer space so the caller can place the camera
        # inside the room. SHARP output flips Y/Z in the viewer, while
        # Nerfstudio exports are already Y-up and opt out with flip_y=false.
        offset = ground_offset or 0.0
        lo, hi = mesh.bounds
        if viewer_mesh is not None:
            viewer_lo, viewer_hi = viewer_mesh.bounds
            bounds_viewer = {
                "min": [float(viewer_lo[0]), float(viewer_lo[1] + offset), float(viewer_lo[2])],
                "max": [float(viewer_hi[0]), float(viewer_hi[1] + offset), float(viewer_hi[2])],
            }
        elif flip_y:
            bounds_viewer = {
                "min": [float(lo[0]), float(-hi[1] + offset), float(-hi[2])],
                "max": [float(hi[0]), float(-lo[1] + offset), float(-lo[2])],
            }
        else:
            bounds_viewer = {
                "min": [float(lo[0]), float(lo[1] + offset), float(lo[2])],
                "max": [float(hi[0]), float(hi[1] + offset), float(hi[2])],
            }

        surfaces = {name: int(len(faces)) for name, faces in groups.items()}
        breakdown = ", ".join(f"{name} {count}" for name, count in surfaces.items())
        print(
            f"Collider built: {len(mesh.faces)} faces, room "
            f"{size[0]:.1f}x{size[1]:.1f}x{size[2]:.1f} m, surfaces: {breakdown}"
        )
        return {
            "collider_glb": base64.b64encode(glb_bytes).decode("ascii"),
            "ground_plane_offset": ground_offset,
            "face_count": int(len(mesh.faces)),
            "room_size": [float(v) for v in size],
            "bounds_viewer": bounds_viewer,
            "surface_face_counts": surfaces,
        }

    except HTTPException:
        # A deliberate 4xx (an oversized upload, an unusable selection) must not
        # be flattened into a 500 by the catch-all below.
        raise
    except Exception as e:
        print(f"Collider generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    # Loopback by default. Nothing here is authenticated, and the endpoints
    # accept multi-gigabyte uploads and occupy the GPU for hours -- on a shared
    # network, binding every interface hands that to anyone who can reach the
    # port. The frontend talks to localhost:8000, so this costs it nothing.
    # Set IMAGEWORLD_BIND_HOST=0.0.0.0 to expose it deliberately.
    host = os.environ.get("IMAGEWORLD_BIND_HOST", "127.0.0.1")
    print(f"Binding to {host}:8000")
    uvicorn.run(app, host=host, port=8000)
