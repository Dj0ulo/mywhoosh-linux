import os
import sys
import tempfile
import zipfile

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

    # 1. Create a secure, self-cleaning temporary directory
    print("Initializing installer setup...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        results = get_download_links('9ndh0f2vhzx2', 'x64', progress_callback=fetch_links_progress_callback)
        pkg = next((r for r in results if 'mywhoosh' in r.get('FileName', '').lower()), None)
        url = pkg['Url']

        print("Downloading installer...")
        msix_temp_path = os.path.join(tmp_dir, pkg['FileName'])
        
        download_file(url, msix_temp_path)
        try:
            extract_file(msix_temp_path, destination_dir)
        except zipfile.BadZipFile:
            print("❌ Error: The downloaded MSIX file was corrupted or incomplete.")
        return False
    
    return True

        
if __name__ == "__main__":
    # Target location inside your user's Wine prefix
    MYWHOOSH_DIR = os.path.expanduser("~/Games/MyWhoosh")
    
    success = download_and_extract_msix(MYWHOOSH_DIR)
    if success:
        print("\nSetup finished perfectly. You can now launch MyWhoosh via Wine!")