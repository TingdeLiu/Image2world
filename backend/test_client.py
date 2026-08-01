import sys
import os
import requests

SERVER_URL = "http://localhost:8000"
TEST_IMAGE_PATH = r"D:\Tyndall Labs\imageworld\public\worlds\home-room\source\0-home.jpg"

def main():
    print("=== ImageWorld Backend Live Test Client ===")
    
    if not os.path.exists(TEST_IMAGE_PATH):
        print(f"[FAIL] Test image not found at: {TEST_IMAGE_PATH}")
        sys.exit(1)
        
    session = requests.Session()
    session.trust_env = False

    # 1. Test Segmentation (SAM 2)
    print("\n[1/3] Testing SAM 2 Segmentation (/api/segment)...")
    try:
        with open(TEST_IMAGE_PATH, "rb") as f:
            files = {"file": f}
            response = session.post(f"{SERVER_URL}/api/segment", files=files, timeout=60)
            
        if response.status_code == 200:
            data = response.json()
            print("[OK] Segmentation Success!")
            print(f"Image Dimensions: {data.get('width')}x{data.get('height')}")
            print(f"Objects detected: {len(data.get('objects', []))}")
            for i, obj in enumerate(data.get('objects', [])[:3]):
                print(f"  - Object {i}: bbox {obj.get('bbox')}, predicted IoU: {obj.get('predicted_iou'):.2f}")
        else:
            print(f"[FAIL] Segmentation Failed: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[FAIL] Segmentation Request Error: {e}")

    # 2. Test 3D Object Generation (TripoSR)
    print("\n[2/3] Testing TripoSR 3D Generation (/api/image-to-3d)...")
    try:
        with open(TEST_IMAGE_PATH, "rb") as f:
            files = {"file": f}
            response = session.post(f"{SERVER_URL}/api/image-to-3d", files=files, timeout=90)
            
        if response.status_code == 200:
            output_glb = "test_output.glb"
            with open(output_glb, "wb") as f_out:
                f_out.write(response.content)
            print(f"[OK] 3D Generation Success! Saved to {output_glb} ({len(response.content)/1024/1024:.2f} MB)")
        else:
            print(f"[FAIL] 3D Generation Failed: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[FAIL] 3D Generation Request Error: {e}")

    # 3. Test SFX Generation (Stable Audio Open)
    print("\n[3/3] Testing Stable Audio Open SFX Generation (/api/generate-sfx)...")
    try:
        data = {
            "prompt": "wood creak collision impact sound",
            "duration": 2.0
        }
        response = session.post(f"{SERVER_URL}/api/generate-sfx", data=data, timeout=90)
            
        if response.status_code == 200:
            output_wav = "test_output.wav"
            with open(output_wav, "wb") as f_out:
                f_out.write(response.content)
            print(f"[OK] SFX Generation Success! Saved to {output_wav} ({len(response.content)/1024:.2f} KB)")
        else:
            print(f"[FAIL] SFX Generation Failed: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[FAIL] SFX Generation Request Error: {e}")

    print("\n=== Live Test Complete ===")

if __name__ == "__main__":
    main()
