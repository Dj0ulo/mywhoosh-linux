#!/usr/bin/python3

import os
import sys
import tempfile
import zipfile
from pathlib import Path
from get_msstore_download_links import get_download_links
from utils import bar_animation, download_file, extract_file


def download_and_extract_msix(destination_dir):
    """
    Downloads a .msix file safely into RAM (/tmp) and extracts 
    its contents directly into the permanent Wine prefix directory.
    """
    # Ensure the target directory inside your Wine prefix exists
    os.makedirs(destination_dir, exist_ok=True)

    def fetch_links_progress_callback(percent):
        """Custom progress bar hook for get_download_links"""
        if percent < 100:       
            sys.stdout.write(f"\r-> Fetching download link: {bar_animation(percent)}")
        else:
            sys.stdout.write(f"\r-> Fetching download link: {bar_animation(100)}\n")
        sys.stdout.flush()

    # 1. Create a cache directory to avoid redownloading on failure
    print("Initializing installer setup...")
    cache_dir = os.path.expanduser("~/.cache/mywhoosh")
    os.makedirs(cache_dir, exist_ok=True)

    results = get_download_links('9ndh0f2vhzx2', 'x64', progress_callback=fetch_links_progress_callback)
    pkg = next((r for r in results if 'mywhoosh' in r.get('FileName', '').lower()), None)
    url = pkg['Url']
    msix_temp_path = os.path.join(cache_dir, pkg['FileName'])

    if os.path.exists(msix_temp_path):
        print(f"The file {msix_temp_path} already exists.")
    else:
        print("Downloading installer...")
        download_file(url, msix_temp_path)

    try:
        extract_file(msix_temp_path, destination_dir)
    except zipfile.BadZipFile:
        print("❌ Error: The downloaded MSIX file was corrupted or incomplete.")
        return False
    
    return True

        
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python mywhoosh.py <directory>")
        exit(1)
    else:
        directory = Path(sys.argv[1])

    success = download_and_extract_msix(directory)
    if success:
        print("\nMyWhoosh downloaded and extracted successfully!")
    else:
        exit(1)