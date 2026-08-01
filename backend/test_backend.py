import sys
import os
# Add TripoSR path
sys.path.append(os.path.join(os.path.dirname(__file__), "TripoSR"))

print("=== ImageWorld Backend Environment Test ===")
print(f"Python Version: {sys.version}")

try:
    import torch
    print(f"PyTorch Version: {torch.__version__}")
    print(f"CUDA Available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"CUDA Device Name: {torch.cuda.get_device_name(0)}")
except ImportError:
    print("[ERROR] PyTorch is not installed!")

try:
    import fastapi
    import uvicorn
    print("[OK] FastAPI and Uvicorn: Installed")
except ImportError:
    print("[ERROR] FastAPI or Uvicorn is not installed!")

try:
    import cv2
    import PIL
    print("[OK] OpenCV and Pillow: Installed")
except ImportError:
    print("[ERROR] OpenCV or Pillow is not installed!")

try:
    from sam2.build_sam import build_sam2
    print("[OK] SAM 2: Installed and Importable")
except ImportError as e:
    print(f"[ERROR] SAM 2 import failed: {e}")

try:
    from simple_lama_inpainting import SimpleLama
    print("[OK] Simple-LaMa: Installed and Importable")
except ImportError as e:
    print(f"[ERROR] Simple-LaMa import failed: {e}")

try:
    from tsr.system import TSR
    print("[OK] TripoSR (TSR): Installed and Importable")
except ImportError as e:
    print(f"[ERROR] TripoSR import failed: {e}")

try:
    import diffusers
    print("[OK] Diffusers (Stable Audio): Installed")
except ImportError:
    print("[ERROR] Diffusers is not installed!")

print("===========================================")
