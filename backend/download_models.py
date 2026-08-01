import os
import urllib.request

def download_file(url, filepath):
    if os.path.exists(filepath):
        print(f"File already exists: {filepath}")
        return
        
    print(f"Downloading {url} to {filepath}...")
    try:
        # Create dir if not exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        # Download with progress indicator
        def report(block_num, block_size, total_size):
            read_so_far = block_num * block_size
            if total_size > 0:
                percent = read_so_far * 100 / total_size
                print(f"Downloaded: {read_so_far/1024/1024:.2f}MB / {total_size/1024/1024:.2f}MB ({percent:.1f}%)", end="\r")
            else:
                print(f"Downloaded: {read_so_far/1024/1024:.2f}MB", end="\r")
                
        urllib.request.urlretrieve(url, filepath, reporthook=report)
        print("\nDownload complete!")
    except Exception as e:
        print(f"Failed to download {url}: {e}")

def main():
    print("=== ImageWorld Local Models Download Script ===")
    
    # Checkpoints directory
    checkpoints_dir = os.path.join(os.path.dirname(__file__), "checkpoints")
    os.makedirs(checkpoints_dir, exist_ok=True)
    
    # 1. SAM 2 Checkpoints (Meta CDN)
    sam2_large_url = "https://dl.fbaipublicfiles.com/segment_anything_2/072824/sam2_hiera_large.pt"
    sam2_base_url = "https://dl.fbaipublicfiles.com/segment_anything_2/072824/sam2_hiera_base_plus.pt"
    
    print("\n--- Downloading SAM 2 Weights ---")
    download_file(sam2_base_url, os.path.join(checkpoints_dir, "sam2_hiera_base_plus.pt"))
    download_file(sam2_large_url, os.path.join(checkpoints_dir, "sam2_hiera_large.pt"))
    
    print("\n=== All weights prepared successfully! ===")
    print("Please follow the setup instructions in backend/README.md to install the model libraries.")

if __name__ == "__main__":
    main()
