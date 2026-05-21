import urllib
import sys
import zipfile
from pathlib import Path

def bar_animation(percent):
    """Simple terminal progress bar animation [=====     ]"""
    bar_length = 30
    filled_length = int(bar_length * percent // 100)
    return f"[{'=' * filled_length}{' ' * (bar_length - filled_length)}] {percent}%"

def download_progress_callback(block_num, block_size, total_size):
    """Custom progress bar hook for urllib"""
    downloaded = block_num * block_size
    if total_size > 0:
        percent = min(100, int(downloaded * 100 / total_size))
        # Convert bytes to Megabytes for cleaner readability
        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = total_size / (1024 * 1024)

        sys.stdout.write(f"\r   {bar_animation(percent)} ({downloaded_mb:.1f}/{total_mb:.1f} MB)")
    else:
        sys.stdout.write(f"\r   {downloaded / (1024 * 1024):.1f} MB downloaded...")
    sys.stdout.flush()

def download_file(url, dest_path, progress_callback=download_progress_callback):
    print(f"-> Downloading file: {Path(dest_path).name}")
    urllib.request.urlretrieve(url, dest_path, progress_callback)
    print("\n✓ Download finished successfully!")
    
def extract_file(zip_path, dest_path):
    print(f"-> Extracting files to: {dest_path}")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        # Get total number of files to show progress if you want, 
        # but standard extractall is fastest.
        zip_ref.extractall(dest_path)
    print("✓ Extraction complete! Game files ready.")
    return True